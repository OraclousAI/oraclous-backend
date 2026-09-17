"""#1006 — a model's tool-call ``args`` that is not a JSON object kills the run on two loop sites
that subscript it with no enclosing ``try``, even though #1004 item 4 already taught the platform
how to turn exactly this shape into an actionable, coded tool error.

``domain/llm/openai_compatible.py`` does ``json.loads(fn.get("arguments") or "{}")`` and catches
only ``JSONDecodeError``/``TypeError`` — a model that returns a JSON array (``["read_file"]``), a
bare string, or ``null`` where its tool's arguments belong sails through as a list/str/None ``args``
and reaches the loop untouched (``tool_use.py:2235`` builds ``tc["args"]`` straight off
``ToolCall.args`` with no shape check of its own).

Two sites in ``_run_tool_calls`` subscript ``tc["args"]`` before the dispatch `try` that would
otherwise catch it:

* the site-restriction gate (#961) — ``_site_violation(tc["args"], policy.required_sites)`` does
  ``args.get("sites")``, reached on any site-restricted run;
* the JSON-repair gate (#853) — ``str(tc["args"].get("source_type", "")) ...`` / a following
  ``tc["args"].get("content")``, reached on a ``requires_valid_json`` member's ``graph-ingest``
  call.

Both raise an uncaught ``AttributeError`` (``list``/``str``/``NoneType`` has no ``.get``) that
propagates out of ``run_tool_use_loop`` and kills the whole run — the exact #693 failure class
#1004 item 4 was raised to close, but only for calls that reach ``dispatch_payload``. Ruled (#1006):
both sites get the SAME fail-closed, coded refusal ``dispatch_payload`` already gives a model whose
args miss the ``dispatch`` boundary entirely — ``NON_OBJECT_ARGUMENTS_REFUSED``
(``"tool_arguments_not_an_object"``) — as a tool-role message, so the run survives and the model is
told what to send instead.

Two other ``tc["args"]`` sites were audited and are OUT of scope:

* ``_call_signature(tc["name"], tc["args"])`` (~line 1658) renders ``args`` through
  ``json.dumps(args, sort_keys=True, default=str)``, which is total over a list/str/None/int/bool —
  it never raises on a non-dict value, so there is nothing to guard.
* ``_url_valued_args(tc["args"], ...)`` (~line 1827) and ``_answered(tc["args"], iteration)``
  (~line 1904) are both gated on ``status == "ok"``, which requires the dispatch `try` at line 1737
  to have SUCCEEDED. The only production ``dispatch`` closure
  (``harness_execution_service.py``'s ``dispatch``) calls ``dispatch_payload`` first, which #1004
  item 4 already made raise ``NonObjectArgumentsRefused`` for any non-dict ``args`` — caught by the
  same `try`, so ``status`` can never be ``"ok"`` when ``args`` is non-dict. Neither site is
  reachable with a non-object ``args`` in the running system.

RED on current ``main`` for the right reason: every test below raises ``AttributeError`` out of
``run_tool_use_loop`` today, not an import error — every symbol used here already exists on `main`
(#1004 shipped in 48e82220 / #961 in the site-restriction gate / #853 in the JSON-repair gate); only
the guard BEHAVIOUR at these two sites is missing.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import LLMResponse, ToolCall, ToolSpec
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.domain.tool_schemas import NON_OBJECT_ARGUMENTS_REFUSED
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]

#: What a model returning something other than a JSON object for `args` actually sends, per
#: `json.loads` on the raw provider payload (#1006, #1004 item 4's own parametrisation).
NON_OBJECT_ARGS = pytest.mark.parametrize(
    "bad_args", [["read_file"], "read_file", None], ids=["list", "string", "null"]
)

SEARCH_BINDING = "research"
NAMED_SITES = ("theverge.com", "bbc.co.uk")

_SEARCH = ToolSpec(
    name="research__search",
    description="search the live web",
    parameters={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "sites": {"type": "array", "items": {"type": "string"}},
        },
    },
    binding=SEARCH_BINDING,
    operation="search",
)

_INGEST = ToolSpec(
    name="graph_ingest__ingest",
    description="ingest content into the knowledge graph",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="graph-ingest",
    operation="ingest",
)


class _CallsBadArgsThenAnswers:
    """Turn 1: the tool named ``tool_name`` with the given (non-dict) ``args``, exactly as the
    provider adapter would hand it to the loop after ``json.loads``. Turn 2 onward: a plain final
    answer, so a run that survives the first turn's guard finishes normally rather than looping."""

    protocol_shape = "fake"

    def __init__(self, tool_name: str, args: Any) -> None:
        self._tool_name = tool_name
        self._args = args
        self.turns = 0
        #: every message the model was shown, across every turn — the last entry after turn 1 is
        #: what the loop fed back for the bad call.
        self.messages_seen: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        self.turns += 1
        self.messages_seen = list(messages)
        if self.turns == 1:
            return LLMResponse(
                text="", tool_calls=[ToolCall("bad-call-1", self._tool_name, self._args)]
            )
        return LLMResponse(text="done, moving on")


class _RecordingDispatch:
    """Records every call that actually reached the registry. A guard that works never adds one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def __call__(self, spec: ToolSpec, args: Any) -> dict[str, Any]:
        self.calls.append((spec.operation, args))
        return {"ok": True}


def _tool_message_for(messages: list[dict[str, Any]], tool_call_id: str) -> dict[str, Any] | None:
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") == tool_call_id:
            return message
    return None


def _env(**overrides: Any) -> PolicyEnvelope:
    kwargs: dict[str, Any] = {
        "max_iterations": 6,
        "max_tool_calls": None,
        "max_wall_time_seconds": None,
        "max_tokens": None,
    }
    kwargs.update(overrides)
    return PolicyEnvelope(**kwargs)


# ── site 1: the #961 site-restriction gate — `_site_violation(tc["args"], ...)` ───────────────────


@NON_OBJECT_ARGS
async def test_site_restriction_gate_survives_non_object_args(bad_args: Any) -> None:
    """A site-restricted run whose model sends a non-object `args` to the gated search must not
    die — the run's `sites` restriction is exactly what makes `_site_violation` the first thing
    to touch `tc["args"]`, with no enclosing `try` above it today."""
    llm = _CallsBadArgsThenAnswers(_SEARCH.name, bad_args)
    dispatch = _RecordingDispatch()

    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="round up this week's tech news",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=_env(required_sites=NAMED_SITES),
        web_search_bindings=frozenset({SEARCH_BINDING}),
    )

    assert result.status is HarnessStatus.SUCCEEDED, (
        f"the run did not survive a non-object args={bad_args!r}: status={result.status}, "
        f"error_type={result.error_type}"
    )


@NON_OBJECT_ARGS
async def test_site_restriction_gate_tells_the_model_the_coded_refusal(bad_args: Any) -> None:
    """Same coded refusal #1004 item 4 already gives a model whose args never reach `dispatch` at
    all — the model must not see two different vocabularies for "you sent the wrong shape"."""
    llm = _CallsBadArgsThenAnswers(_SEARCH.name, bad_args)
    dispatch = _RecordingDispatch()

    await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="round up this week's tech news",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=_env(required_sites=NAMED_SITES),
        web_search_bindings=frozenset({SEARCH_BINDING}),
    )

    tool_message = _tool_message_for(llm.messages_seen, "bad-call-1")
    assert tool_message is not None, "the model was never handed a tool-role reply for its call"
    assert NON_OBJECT_ARGUMENTS_REFUSED in tool_message["content"]


@NON_OBJECT_ARGS
async def test_site_restriction_gate_never_dispatches_a_non_object_call(bad_args: Any) -> None:
    llm = _CallsBadArgsThenAnswers(_SEARCH.name, bad_args)
    dispatch = _RecordingDispatch()

    await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="round up this week's tech news",
        tool_specs=[_SEARCH],
        dispatch=dispatch,
        policy=_env(required_sites=NAMED_SITES),
        web_search_bindings=frozenset({SEARCH_BINDING}),
    )

    assert dispatch.calls == []
