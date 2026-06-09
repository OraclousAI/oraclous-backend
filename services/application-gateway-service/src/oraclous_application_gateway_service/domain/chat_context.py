"""Chat context serialization (ORAA-4 §21 domain layer) — pure.

The harness runs ONE OHM against ONE ``input`` per execute (no conversation field), so a chat
turn folds the prior turns + the new user message into one delimited transcript block, capped by
turns and an approximate char/token budget (most-recent turns kept; oldest dropped first;
the harness applies its own iteration/token envelope on top).
"""

from __future__ import annotations

_MAX_TURNS = 20
_MAX_CHARS = 24000  # coarse budget (~chars/4 tokens) so the transcript fits one harness input

_ROLE_LABEL = {"user": "User", "assistant": "Assistant"}


def build_turn_input(history: list[tuple[str, str]], new_message: str) -> str:
    """``history`` is oldest->newest ``(role, content)`` for role in user/assistant. Returns the
    harness ``input``: the most-recent (turn- and size-capped) transcript + the new user message."""
    kept: list[tuple[str, str]] = []
    used = len(new_message)
    for role, content in reversed(history[-_MAX_TURNS * 2 :]):
        label = _ROLE_LABEL.get(role)
        if label is None:  # system / unknown roles are not replayed into the agent
            continue
        line = f"{label}: {content}"
        if used + len(line) > _MAX_CHARS:
            break
        kept.append((role, content))
        used += len(line)
    if not kept:
        return new_message
    kept.reverse()
    transcript = "\n".join(f"{_ROLE_LABEL[r]}: {c}" for r, c in kept)
    return f"Conversation so far:\n{transcript}\n\nCurrent message:\n{new_message}"


def derive_title(first_message: str, *, max_len: int = 80) -> str:
    """A thread's title is the first line of its first user message (truncated)."""
    stripped = first_message.strip()
    if not stripped:
        return "New chat"
    first_line = stripped.splitlines()[0]
    return first_line[:max_len]
