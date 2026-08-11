"""#743 — the run records the set of ``citation_id``s the platform served to it (§CITE).

``served_citation_ids`` is a RESERVED result key, the ``data_absent`` (#580) pattern: the retrieval
connector sets it, the loop POPS it before the result reaches the model, and the loop accumulates it
across the whole run. Two properties fall out, and both are the point:

* The model never reads the key, so it can never learn what shape platform bookkeeping has.
* The set is per RUN, not per tool call — a member retrieves several times before it answers — and
  it deduplicates, because ``citation_id`` is deterministic and one document version has one id.

The citation itself moves the other way. It stays INSIDE the tool content the model reads, because a
member asked to cite its sources has to be shown them (#642): a receipt the model cannot see is a
trap, and real models were failed for guessing at ids they were never shown.

Covers acceptance criteria 1 (loop half), 2, 3, 5 and 6 of #743. The forged-key injection is a
security test and lives in ``test_loop_served_citations_injection.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]

_CIT_A = "cit_9f2a4c81b7d3e50a1c6f28934bd5e7a0"
_CIT_B = "cit_0b1d3f57a9c2e4680d8f1a3b5c7e9021"
_CIT_C = "cit_4e6a8c02b1d3f5079a2c4e6081b3d5f7"

_ENVELOPE = PolicyEnvelope(
    max_iterations=6, max_tool_calls=None, max_wall_time_seconds=None, max_tokens=None
)

_RETRIEVE = ToolSpec(
    name="kr__search",
    description="search the knowledge graph",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="knowledge-retriever",
    operation="search",
)


def _hit(node_id: str, citation_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "Chunk",
        "properties": {"text": "the parties agreed to a 30-day notice period"},
        "citation": {
            "citation_id": citation_id,
            "source_system": "github",
            "source_id": f"OraclousAI/oraclous-backend:{node_id}.md",
            "revision": "e3b0c44298fc1c149afbf4c8996fb924",
            "revision_kind": "blob_sha",
            "url": f"https://github.com/OraclousAI/oraclous-backend/blob/e3b0c44/{node_id}.md",
            "title": f"{node_id}.md",
            "author": {"display_name": "Parham Davari"},
            "retrieved_at": "2026-08-08T09:14:22Z",
            "permission_ref": None,
        },
    }


def _serving(*citation_ids: str) -> Any:
    """A retrieval dispatch serving one hit per id, with the reserved key the connector emits."""

    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        return {
            "hits": [_hit(f"n{i}", cid) for i, cid in enumerate(citation_ids)],
            "served_citation_ids": list(citation_ids),
        }

    return dispatch


class _CallsThenAnswers:
    """Calls the retrieval tool ``turns`` times (``per_turn`` calls each), then answers."""

    protocol_shape = "fake"

    def __init__(self, turns: int = 1, per_turn: int = 1) -> None:
        self._turns = turns
        self._per_turn = per_turn
        self._seen = 0
        self.tool_content: list[str] = []

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        self.tool_content = [str(m.get("content", "")) for m in messages if m.get("role") == "tool"]
        self._seen += 1
        if self._seen > self._turns:
            return LLMResponse(text="here is the answer", tool_calls=[])
        calls = [
            ToolCall(f"c{self._seen}-{i}", _RETRIEVE.name, {"query": "q"})
            for i in range(self._per_turn)
        ]
        return LLMResponse(text="searching", tool_calls=calls)


class _AnswersImmediately:
    """A reasoning-only member: answers on turn one, never calls a tool."""

    protocol_shape = "fake"

    async def complete(self, *, messages: Any, system: str, tools: list[ToolSpec]) -> LLMResponse:
        return LLMResponse(text="I already know this", tool_calls=[])


async def _run(llm: Any, dispatch: Any, *, specs: list[ToolSpec] | None = None) -> Any:
    return await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="what did we agree",
        tool_specs=specs if specs is not None else [_RETRIEVE],
        dispatch=dispatch,
        policy=_ENVELOPE,
    )


# --- criteria 1 + 2: the citation is shown, the reserved key is popped -----------------------


async def test_the_model_reads_the_citation_of_every_hit() -> None:
    # #642: the member is asked to cite its sources, so the ids it may cite have to be in front of
    # it. The citation travels inside the tool content, not in transport metadata it cannot see.
    llm = _CallsThenAnswers()
    await _run(llm, _serving(_CIT_A, _CIT_B))
    assert any(_CIT_A in c and _CIT_B in c for c in llm.tool_content)


async def test_the_reserved_key_never_reaches_the_model() -> None:
    # The platform's bookkeeping is popped before the result is serialised for the model, exactly
    # as data_absent is. The model cannot read the key, so it cannot learn to write one.
    llm = _CallsThenAnswers()
    await _run(llm, _serving(_CIT_A))
    assert llm.tool_content  # the tool really ran — an empty transcript would pass vacuously
    assert all("served_citation_ids" not in c for c in llm.tool_content)


# --- criteria 3 + 5: accumulation across the run, deduplicated ------------------------------


async def test_the_set_accumulates_across_several_calls_in_one_turn() -> None:
    llm = _CallsThenAnswers(turns=1, per_turn=2)
    result = await _run(llm, _serving(_CIT_A, _CIT_B))
    assert result.status is HarnessStatus.SUCCEEDED
    assert set(result.served_citation_ids) == {_CIT_A, _CIT_B}


async def test_the_set_accumulates_across_several_iterations() -> None:
    # The set is per RUN: a member retrieves, reads, retrieves again, and only then answers. An
    # id served on iteration one is still citable in the answer written on iteration three.
    served = [[_CIT_A], [_CIT_B], [_CIT_C]]

    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        ids = served.pop(0)
        return {"hits": [_hit("n", ids[0])], "served_citation_ids": ids}

    result = await _run(_CallsThenAnswers(turns=3), dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert set(result.served_citation_ids) == {_CIT_A, _CIT_B, _CIT_C}


async def test_the_same_record_served_twice_is_one_entry() -> None:
    # citation_id is deterministic, so re-reading one document version yields the same id. The run
    # records WHAT it served, not how many times — a duplicate is not a second source.
    result = await _run(_CallsThenAnswers(turns=3), _serving(_CIT_A))
    assert list(result.served_citation_ids) == [_CIT_A]


# --- criterion 6: a run that retrieved nothing carries an EMPTY set --------------------------


async def test_a_run_with_no_retrieval_carries_an_empty_set() -> None:
    # Empty, never None: the answer-time gate reads this set on every run, and a None would make
    # "served nothing" indistinguishable from "the loop forgot to record" at the one moment it
    # matters. A reasoning-only member is a normal run, not an error.
    async def _unused(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no tool should be dispatched")

    result = await _run(_AnswersImmediately(), _unused)
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.served_citation_ids is not None
    assert len(result.served_citation_ids) == 0


async def test_a_retrieval_that_cited_nothing_carries_an_empty_set() -> None:
    # Records ingested before §CITE carry citation: null, so the connector emits no reserved key.
    # The run served nothing citable and says so plainly — it is not an error and not a None.
    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        return {"hits": [{"id": "n1", "type": "Chunk", "properties": {}, "citation": None}]}

    result = await _run(_CallsThenAnswers(), dispatch)
    assert result.status is HarnessStatus.SUCCEEDED
    assert len(result.served_citation_ids) == 0


# --- criterion 5, the degrade path: what was served survives a degraded run ------------------


async def test_a_degraded_run_still_carries_what_it_served() -> None:
    # #580: a later empty retrieval degrades the member to a flagged PARTIAL. The citations already
    # served are still real, and the answer still cites them — the gate must be able to check it.
    results: list[dict[str, Any]] = [
        {"hits": [_hit("n0", _CIT_A)], "served_citation_ids": [_CIT_A]},
        {"hits": [], "data_absent": True},
    ]

    async def dispatch(_spec: ToolSpec, _args: dict[str, Any]) -> dict[str, Any]:
        return results.pop(0)

    result = await _run(_CallsThenAnswers(turns=2), dispatch)
    assert result.status is HarnessStatus.PARTIAL
    assert result.error_type == "empty_retrieval"
    assert set(result.served_citation_ids) == {_CIT_A}
