"""#1072: the tool-use loop's live progress, shared with a caller running it as a cancellable
``asyncio.Task``.

A cancelled task's coroutine never reaches its own ``return`` — whatever ``run_tool_use_loop`` would
have handed back in its ``LoopResult`` is lost with it. ``LoopProgress`` is a small MUTABLE object
the loop writes into synchronously, in the same task, as it goes (after every LLM response and every
step append), so a caller that cancels the loop mid-flight can still see exactly what was already
booked and persist a ``CANCELLED`` terminal from that, not from nothing (design doc, #1072: "Spend
survives cancellation").

Every field here mirrors a ``LoopResult`` field the harness service reads when it persists a run's
terminal row, so the service can build a ``CANCELLED`` row from ``progress`` alone, without ever
seeing the ``LoopResult`` a cancelled run will never return.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Deferred (never imported at runtime): ``tool_use.py`` imports ``LoopProgress`` from this
    # module, so an eager import here would be circular. `from __future__ import annotations` keeps
    # this file's own annotations as strings, so the class only needs to be importable to a type
    # checker, never to the interpreter.
    from oraclous_harness_runtime_service.domain.loop.tool_use import LoopStep


@dataclass(slots=True)
class LoopProgress:
    """Live, mutable spend + trace for one in-flight ``run_tool_use_loop`` call.

    ``total_tokens``/``prompt_tokens``/``completion_tokens`` and ``iterations`` are booked once per
    completed LLM turn (never mid-turn), so a cancellation during an in-flight ``llm.complete`` call
    never reports that turn's tokens — only every turn that already finished.

    ``steps``, ``served_citation_ids``, and ``fetched_urls`` are the SAME list objects the loop
    itself builds and appends to (aliased, not copied, the moment the loop creates them) — every
    append the loop makes is visible here immediately, with no separate sync step that a
    cancellation could land between.
    """

    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    iterations: int = 0
    steps: list[LoopStep] = field(default_factory=list)
    served_citation_ids: list[str] = field(default_factory=list)
    fetched_urls: list[str] = field(default_factory=list)
    # #907: which LLM client ran this segment (the client's own ``protocol_shape``) — set once,
    # before the loop's first turn, since the loop's client never changes mid-run.
    protocol_shape: str | None = None
