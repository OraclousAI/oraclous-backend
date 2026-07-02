"""Seeded-refresh 5-way what-changed delta (domain layer; #602, ADR-048 decision 3).

A refresh run seeds a NAMED prior run's output and emits, beside the verdict, a first-class
**5-way delta** — each record classified ``added | removed | changed | unchanged | re_confirmed``.
This module is the PURE, deterministic classifier (no I/O, no DB): given the prior (seed) records
and this run's fresh records, it computes the delta by record IDENTITY + a per-record EVIDENCE
FINGERPRINT (a content hash — NOT a clock, ADR-048 §3 reject (c)).

The two distinctions that carry the contract (Lock O3 — "never silently worse"):

* ``unchanged`` (SKIPPED — its producer was NOT re-run) is DISTINCT from ``re_confirmed``
  (re-examined and still true). Conflating them is the forbidden "silently worse" failure. Because
  the engine executes MEMBERS not records, the SKIP signal comes from the producing member (it
  re-emits a carried-forward record marked ``refresh_status: unchanged``); the engine credits
  ``unchanged`` only on an explicit skip marker + a fingerprint match. **FAIL-OPEN:** a fingerprint
  match WITHOUT a skip marker is ``re_confirmed`` (re-examined), never a false ``unchanged`` — an
  uncertain record is never silently claimed skipped. A fingerprint MISMATCH is ``changed``
  regardless of any marker.

* Record identity is a STABLE key (``id`` field by default), never list position — an EURail ledger
  adds/removes rows, so an index would re-classify everything after an edit as changed.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# an LLM member often wraps its JSON deliverable in a ```json … ``` markdown fence; strip it so the
# ledger still parses (the fence is transport, not evidence).
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
# … and often reasons in PROSE first, then emits the ledger in a ```json … ``` block partway
# through (#602 carry-forward-vs-derive: a refresh echoes tersely, a cold run reasons then emits).
# When the whole deliverable is not itself parseable, extract the first fenced block that IS a
# record array. The classification is unchanged — this only makes record EXTRACTION robust to prose.
_EMBEDDED_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _record_hash(evidence: dict[str, Any]) -> str:
    """SHA-256 of the record's canonical evidence JSON (sorted keys). A LOCAL hash — deliberately
    NOT ``oraclous_ohm.canonical.content_hash``, which strips a top-level ``signatures`` key (built
    for SIGNED OHM documents) and would silently exclude an arbitrary record field named
    ``signatures`` from the fingerprint — a false-``unchanged`` soundness hole (Lock O3). This
    hashes the WHOLE evidence dict; the only exclusion is ``refresh_status`` (dropped elsewhere)."""
    return hashlib.sha256(
        json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        ).encode("utf-8")
    ).hexdigest()


# the reserved key under which the seed records ride into the run's ``inputs`` (the #599 state
# seam), so the producing member can carry-forward unchanged records (the cost lever).
REFRESH_SEED_KEY = "_refresh_seed"
# the per-record marker the producing member sets to CLAIM a skip (carried forward, not re-derived).
REFRESH_STATUS_FIELD = "refresh_status"
_SKIP_MARKERS = frozenset({"unchanged", "carried", "carried_forward", "skip", "skipped"})

# the 5 classes (ADR-048 §3 — a binary changed/unchanged is explicitly rejected)
ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"
UNCHANGED = "unchanged"
RE_CONFIRMED = "re_confirmed"


def _record_list(parsed: Any) -> list[dict[str, Any]] | None:  # noqa: ANN401
    # keep only dict rows — a scalar/array row has no stable identity + cannot be fingerprinted
    if not isinstance(parsed, list):
        return None
    return [r for r in parsed if isinstance(r, dict)]


def _loads_record_list(text: str) -> list[dict[str, Any]] | None:
    try:
        return _record_list(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        return None


def parse_records(deliverable: Any) -> list[dict[str, Any]] | None:  # noqa: ANN401
    """Parse a producing member's deliverable into a list of record dicts, or ``None`` when the
    deliverable is not a JSON array of objects (then there is no per-record delta to compute — the
    caller records an empty/absent delta rather than a false one). Mirrors the shipped
    ``count_ledger_records`` json.loads model so the delta + the eval oracle agree on "a record"."""
    if isinstance(deliverable, list):
        return _record_list(deliverable)
    if not isinstance(deliverable, str):
        return None
    text = deliverable.strip()
    whole = _FENCE_RE.match(
        text
    )  # the whole deliverable IS (a fenced) JSON array — the strict path
    direct = _loads_record_list(whole.group(1).strip() if whole else text)
    if direct is not None:
        return direct
    # the member reasoned in prose then emitted the ledger in a ```json fence — take the LAST fenced
    # block that is a NON-EMPTY record array. The ledger is emitted "at the very end" (after any
    # illustrative example / per-item fences the model may show first), so the last one is the real
    # deliverable. ``if block`` (non-empty), NOT ``is not None`` (#602 review Finding 2): a TRAILING
    # non-object array a member may append after the ledger (e.g. a "sources" list of url strings)
    # dict-filters to ``[]`` — it must NEVER override the real ledger and zero the delta/cost-lever.
    last: list[dict[str, Any]] | None = None
    for m in _EMBEDDED_FENCE_RE.finditer(text):
        block = _loads_record_list(m.group(1).strip())
        if block:  # a non-empty record array only; a trailing scalar fence (→ []) can never win
            last = block
    return last


def _identity(record: dict[str, Any], id_field: str) -> str:
    """The stable per-record key: the ``id_field`` value if present + scalar, else the record's full
    content hash (a no-id record is identified by content → an edit reads as removed+added, never a
    false unchanged)."""
    val = record.get(id_field)
    if isinstance(val, (str, int, float, bool)):
        # type-qualify: a str "1.0", a float 1.0 and an int 1 are DISTINCT records, never one key
        # (an un-typed key would collide them and silently drop one as a false removed/unchanged).
        return f"{id_field}={type(val).__name__}:{val!r}"
    return f"@{_record_hash(_evidence(record))}"


def _evidence(record: dict[str, Any]) -> dict[str, Any]:
    """The record's evidence content the fingerprint hashes — the record MINUS the transport-only
    ``refresh_status`` marker (a member's skip claim must never change the evidence hash)."""
    return {k: v for k, v in record.items() if k != REFRESH_STATUS_FIELD}


def _fingerprint(record: dict[str, Any]) -> str:
    """Evidence fingerprint = SHA-256 of the record's canonical evidence content (ADR-048 §3: a
    content/evidence hash, NOT a timestamp)."""
    return _record_hash(_evidence(record))


def _claims_skip(record: dict[str, Any]) -> bool:
    marker = record.get(REFRESH_STATUS_FIELD)
    return isinstance(marker, str) and marker.strip().lower() in _SKIP_MARKERS


def compute_delta(
    seed_records: list[dict[str, Any]],
    fresh_records: list[dict[str, Any]],
    *,
    id_field: str = "id",
) -> dict[str, Any]:
    """Classify every record into exactly one of the 5 classes by identity + evidence fingerprint.

    added: a fresh id absent from the seed. removed: a seed id absent from the fresh output.
    changed: same id, fingerprint MOVED. unchanged: same id, fingerprint MATCH, AND the fresh record
    explicitly claims a skip (carried forward, producer not re-run). re_confirmed: same id, fp
    MATCH, WITHOUT a skip claim (re-examined, still true) — the fail-open default so an uncertain
    record is never falsely credited as skipped (Lock O3). A DUPLICATE fresh id (a member emitting
    the same identity twice — e.g. a carried skip-copy AND a re-derived copy) fails CLOSED to
    ``changed`` and is NEVER credited a skip, so emit-order can never smuggle a moved record through
    as ``unchanged``. Returns the full 5-way delta + counts + ``skipped`` (the unchanged count — the
    cost-saving signal) + the duplicate-id counts (a lossy-input signal, never silent)."""
    # keep first per seed id (a seed dup is the prior run's shape; note it, don't drop ``removed``)
    seed_by_id: dict[str, dict[str, Any]] = {}
    seed_dupes = 0
    for r in seed_records:
        rid = _identity(r, id_field)
        if rid in seed_by_id:
            seed_dupes += 1
        else:
            seed_by_id[rid] = r
    # GROUP fresh by identity (do not collapse with setdefault) so a duplicate id is detected, not
    # silently dropped — a dropped changed-copy would masquerade as an unchanged skip (Lock O3).
    fresh_groups: dict[str, list[dict[str, Any]]] = {}
    for r in fresh_records:
        fresh_groups.setdefault(_identity(r, id_field), []).append(r)

    out: dict[str, list[dict[str, Any]]] = {
        ADDED: [],
        REMOVED: [],
        CHANGED: [],
        UNCHANGED: [],
        RE_CONFIRMED: [],
    }
    for rid, group in fresh_groups.items():
        fresh = group[0]
        seed = seed_by_id.get(rid)
        if len(group) > 1:
            out[CHANGED].append(fresh)  # duplicate id → fail CLOSED to changed, never a skip
        elif seed is None:
            out[ADDED].append(fresh)
        elif _fingerprint(fresh) != _fingerprint(seed):
            out[CHANGED].append(fresh)  # evidence moved → re-derived to a different value
        elif _claims_skip(fresh):
            out[UNCHANGED].append(fresh)  # fp match + explicit skip claim → carried forward
        else:
            out[RE_CONFIRMED].append(fresh)  # fp match, no skip claim → re-examined, still true
    for rid, seed in seed_by_id.items():
        if rid not in fresh_groups:
            out[REMOVED].append(seed)

    counts = {k: len(v) for k, v in out.items()}
    return {
        **out,
        "counts": counts,
        "skipped": counts[UNCHANGED],  # records whose producer was NOT re-run (the cost win)
        "id_field": id_field,
        "duplicate_fresh_ids": sum(1 for g in fresh_groups.values() if len(g) > 1),
        "duplicate_seed_ids": seed_dupes,
    }
