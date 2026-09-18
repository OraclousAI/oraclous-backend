"""The platform's own settle-time save of a member's deliverable (#1137/#1142, domain layer).

Validation Desk's decision brief was lost on 5 of 5 runs: the ONLY thing that put a member's answer
on the team graph was the model remembering to call ``graph-ingest`` at the end of its loop, and
nothing enforced that it did. A bound save tool is a menu, not an intent. So the platform decides
for itself, at settle, from data the engine already holds — never a second trust in the model's
tool-calling.

This module is pure: a trigger predicate, the document shape, and the duplicate-guard predicate. It
performs no I/O and mints no identity; the service layer does the artifact read, the send, and the
provenance emit, and hands this module what it already has.

Two of the three historical save failures were ingestion mechanics, and both are avoided here BY
CONSTRUCTION:

  * ``Neo.ClientError.Statement.TypeError`` (run ``28fcb3f6``): a STRUCTURED ``source_type`` routed
    a brief's nested ``hypotheses[]`` objects onto Neo4j node properties, which accept only
    primitives and arrays of them. :func:`build_document` always emits ``source_type="text"`` and
    serialises any nested value INSIDE the content string, so the structured recipe path is never
    entered at all.
  * an un-parseable, truncated JSON tool argument (run ``a50dc40d``): a model-authored argument.
    The content here is re-serialised from the already-validated, already-stored settle payload, so
    there is no model-authored text to truncate.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from oraclous_execution_engine_service.domain.member_answer import parse_member_object

#: A member SETTLED with a real answer. ``partial`` belongs here: #1015's live run ``6332197e``
#: settled ``partial`` (an ``empty_retrieval`` note) carrying a genuine posture/headline — exactly
#: the deliverable this issue is about losing. Every other status either never settled or settled
#: with nothing to save.
_SETTLED_STATUSES = frozenset({"succeeded", "partial"})

#: The document type the platform ALWAYS writes. Never a structured type — see the module docstring.
_SOURCE_TYPE = "text"


def should_autosave(
    *,
    graph_id: str | None,
    status: str,
    payload: dict[str, Any] | None,
    declared_keys: Sequence[str],
    is_fan_out: bool,
) -> bool:
    """Whether the platform saves this settled member's deliverable to the team graph.

    All of, else ``False``:

    * the run is BOUND to a graph — with no graph there is nowhere to write;
    * the member is not a FAN-OUT member. Its sub-runs share one role, and the per-item ``ordinal``
      that would disambiguate their artifacts is not recoverable at settle (#1015), so v1 skips
      them explicitly rather than writing indistinguishable documents. Documented, never silent;
    * it SETTLED with a non-null result, ``succeeded`` or ``partial``;
    * its manifest DECLARED required output keys — an undeclared member (every team compiled before
      #697) has no defined deliverable shape, and the platform never invents one;
    * every declared key is actually PRESENT in the settled payload — a member that promised a key
      and did not deliver it has nothing there to write, and the platform never fabricates it.
    """
    if graph_id is None:
        return False
    if is_fan_out:
        return False
    if status not in _SETTLED_STATUSES:
        return False
    if not declared_keys:
        return False
    if not isinstance(payload, dict):
        return False
    return all(key in payload for key in declared_keys)


def build_document(
    *,
    payload: dict[str, Any],
    declared_keys: Sequence[str],
    producer: dict[str, Any],
) -> dict[str, Any]:
    """The document the platform writes for one settled member.

    PRECONDITION: every key in ``declared_keys`` is present in ``payload``. This is not a defensive
    function — :func:`should_autosave` has already established exactly that, and the service layer
    calls it first and only writes when it returns ``True``. A caller that breaks the precondition
    gets a ``KeyError`` rather than a quietly truncated document, which is the right failure: a
    deliverable missing a key it promised is the thing the trigger exists to refuse, and silently
    shipping a partial one to the graph would look like a successful save.

    ``content`` is the member's WHOLE final answer object (#1142), with the declared keys — already
    lifted onto the payload, and guaranteed present by :func:`should_autosave` — unioned over it.
    Run ``925dce0e`` settled a member whose answer carried nine keys and saved a 172-character
    document holding the two its manifest happened to declare; the brief page renders straight off
    this content, so its reasoning, sections and "what would change this" items never existed as far
    as a reader was concerned. The answer is PEELED out of ``payload["output"]`` (the model's raw
    reply: prose, a fence, or bare JSON) by the same
    :func:`~oraclous_execution_engine_service.domain.member_answer.parse_member_object` the
    orchestrator peels a producer's hand-off with, so a trailing ``driving_signals`` receipt object
    is never the object that wins.

    What the member said is saved; what the RUN said about it is not. The surrounding envelope —
    ``status``, ``simulated``, ``fetched_urls``, ``unverified_links``, ``output``, ``steps``, the
    harness's ``error_type``/``error_message`` — is the run's own bookkeeping, never the member's
    answer, and is excluded by construction: only the peeled object and the declared keys are read.
    Keys the member itself put INSIDE its answer object are its own content and are kept.

    FLOOR: a member whose answer does not peel to a JSON object at all (no ``output``, prose with no
    embedded object, a bare array) still saves exactly its declared keys — #1137's original
    behaviour, so nothing regresses to writing nothing.

    A nested value round-trips intact, carried as text rather than structure.

    ``title`` is deliberately ``None``: the KGS's ``derive_name`` then falls back to the producing
    member's role, which is the name a person looking at the graph wants.

    ``producer`` is passed through VERBATIM. This module mints no identity of its own — the stamp is
    whatever the caller built (the same shape ``team_run.py::_producer_ref`` mints for the tool
    path), and the client filters it to the wire fields the ingest route reads.
    """
    ordered_keys = list(dict.fromkeys(declared_keys))
    declared = {key: payload[key] for key in ordered_keys}
    answer = parse_member_object(payload.get("output"), declared_keys=ordered_keys)
    content = {**answer, **declared}
    return {
        "content": json.dumps(content, ensure_ascii=False, indent=2),
        "source_type": _SOURCE_TYPE,
        "title": None,
        "producer": producer,
    }


def should_skip_as_duplicate(existing: list[dict[str, Any]] | None) -> bool:
    """Whether an artifact listing for this run + member role makes the platform's save redundant.

    Any row whose ``status`` is not ``"failed"`` suppresses the write — that one signal covers both
    a model ``graph-ingest`` call that really landed and the platform's own earlier attempt. A
    content hash cannot be the key here: the tool writes the model's prose and the platform writes
    canonical JSON of the member's answer object, so a real prior save and this write never
    hash-match. Rows that are ALL ``"failed"`` do not suppress it — a failed attempt is not a save.

    ``None`` means the listing itself was INCONCLUSIVE (the read errored), and the write proceeds:
    ruled on #1137, a duplicate is recoverable while a lost deliverable is the defect being fixed.
    The service layer collapses a failed read to ``None`` — never to ``[]``, which means "listed
    successfully, found nothing" — which is what lets this stay a pure predicate.
    """
    if not existing:
        return False
    return any(row.get("status") != "failed" for row in existing)
