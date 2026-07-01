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

import json
from typing import Any

from oraclous_ohm.canonical import content_hash

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


def parse_records(deliverable: Any) -> list[dict[str, Any]] | None:  # noqa: ANN401
    """Parse a producing member's deliverable into a list of record dicts, or ``None`` when the
    deliverable is not a JSON array of objects (then there is no per-record delta to compute — the
    caller records an empty/absent delta rather than a false one). Mirrors the shipped
    ``count_ledger_records`` json.loads model so the delta + the eval oracle agree on "a record"."""
    if isinstance(deliverable, list):
        parsed: Any = deliverable
    elif isinstance(deliverable, str):
        try:
            parsed = json.loads(deliverable)
        except (json.JSONDecodeError, TypeError):
            return None
    else:
        return None
    if not isinstance(parsed, list):
        return None
    # keep only dict rows — a scalar/array row has no stable identity + cannot be fingerprinted
    return [r for r in parsed if isinstance(r, dict)]


def _identity(record: dict[str, Any], id_field: str) -> str:
    """The stable per-record key: the ``id_field`` value if present + scalar, else the record's full
    content hash (a no-id record is identified by content → an edit reads as removed+added, never a
    false unchanged)."""
    val = record.get(id_field)
    if isinstance(val, (str, int, float, bool)):
        return f"{id_field}={val}"
    return f"@{content_hash(_evidence(record))}"


def _evidence(record: dict[str, Any]) -> dict[str, Any]:
    """The record's evidence content the fingerprint hashes — the record MINUS the transport-only
    ``refresh_status`` marker (a member's skip claim must never change the evidence hash)."""
    return {k: v for k, v in record.items() if k != REFRESH_STATUS_FIELD}


def _fingerprint(record: dict[str, Any]) -> str:
    """Evidence fingerprint = SHA-256 of the record's canonical evidence content (ADR-048 §3: a
    content/evidence hash, NOT a timestamp)."""
    return content_hash(_evidence(record))


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
    record is never falsely credited as skipped (Lock O3). Returns the full 5-way delta + counts +
    ``skipped`` (the unchanged count — the cost-saving signal)."""
    seed_by_id: dict[str, dict[str, Any]] = {}
    for r in seed_records:
        seed_by_id.setdefault(_identity(r, id_field), r)
    fresh_by_id: dict[str, dict[str, Any]] = {}
    for r in fresh_records:
        fresh_by_id.setdefault(_identity(r, id_field), r)

    out: dict[str, list[dict[str, Any]]] = {
        ADDED: [],
        REMOVED: [],
        CHANGED: [],
        UNCHANGED: [],
        RE_CONFIRMED: [],
    }
    for rid, fresh in fresh_by_id.items():
        seed = seed_by_id.get(rid)
        if seed is None:
            out[ADDED].append(fresh)
        elif _fingerprint(fresh) != _fingerprint(seed):
            out[CHANGED].append(fresh)  # evidence moved → re-derived to a different value
        elif _claims_skip(fresh):
            out[UNCHANGED].append(fresh)  # fp match + explicit skip claim → carried forward
        else:
            out[RE_CONFIRMED].append(fresh)  # fp match, no skip claim → re-examined, still true
    for rid, seed in seed_by_id.items():
        if rid not in fresh_by_id:
            out[REMOVED].append(seed)

    counts = {k: len(v) for k, v in out.items()}
    return {
        **out,
        "counts": counts,
        "skipped": counts[UNCHANGED],  # records whose producer was NOT re-run (the cost win)
        "id_field": id_field,
    }
