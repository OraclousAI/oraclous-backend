"""LLM client seam (domain layer; ADR-007).

The tool-use loop talks to one ``LLMClient`` interface and never branches on provider. Each concrete
client owns its own message/tool marshalling and declares its ``protocol_shape`` (native /
openai-compatible / gemini-compatible). A ``ToolSpec`` is the shape-agnostic description of
one callable capability operation; the client converts it to its wire shape, and the loop maps a
returned ``ToolCall`` back to a registry dispatch via the spec's ``binding`` + ``operation``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# A neutral message in the loop's transcript. role ∈ {system, user, assistant, tool}.
Message = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One LLM-callable tool = one capability operation. ``name`` is what the model calls;
    ``binding``/``operation`` are how the loop dispatches it to the registry.

    ``strict``/``nullable_keys`` (#898) are carried EXPLICITLY, never inferred from ``parameters``'
    own shape — a schema that already looks fully-required-and-closed must still stay non-strict
    unless something upstream set the marker. ``strict`` tells the wire client to set the
    provider's strict function-calling flag; ``nullable_keys`` names every property the PLATFORM
    itself widened to accept ``null`` when rendering ``parameters`` (a previously optional or
    instance-bound argument) — ``dispatch_payload`` strips a null on exactly those keys, and only
    those, before the registry ever sees the call."""

    name: str
    description: str
    parameters: dict[str, Any]
    binding: str
    operation: str
    strict: bool = False
    nullable_keys: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    """A single completion: free text and/or tool calls. When ``tool_calls`` is non-empty the loop
    dispatches each and feeds results back; when empty, ``text`` is the final answer."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    total_tokens: int = 0  # metered token usage for this call (0 when the provider omits it / fake)
    # The input/output split of that usage (prompt vs completion). Output tokens cost ~3-4× input,
    # so the split is what lets spend be priced honestly (ADR-009 stays raw; pricing is a read-time
    # layer). 0 when the provider omits the split / fake mode — total_tokens is still kept.
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class LLMClient(Protocol):
    protocol_shape: str

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse: ...
