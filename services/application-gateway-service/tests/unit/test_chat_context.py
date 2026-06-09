"""Unit: the pure chat-context serialization — history folded into one input, capped + titled."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.domain.chat_context import (
    build_turn_input,
    derive_title,
)

pytestmark = pytest.mark.unit


def test_no_history_is_just_the_message() -> None:
    assert build_turn_input([], "hello") == "hello"


def test_history_is_folded_into_the_input_oldest_first() -> None:
    history = [("user", "what is 2+2"), ("assistant", "4"), ("user", "and times 3")]
    out = build_turn_input(history, "are you sure?")
    assert "Conversation so far:" in out
    assert (
        out.index("User: what is 2+2") < out.index("Assistant: 4") < out.index("User: and times 3")
    )
    assert out.rstrip().endswith("Current message:\nare you sure?")


def test_system_role_is_not_replayed() -> None:
    out = build_turn_input([("system", "secret prompt"), ("user", "hi")], "next")
    assert "secret prompt" not in out and "User: hi" in out


def test_size_cap_drops_oldest_turns() -> None:
    big = "x" * 20000
    history = [("user", big), ("assistant", big)]  # together exceed the ~24k char budget
    out = build_turn_input(history, "now")
    # the most-recent turn is kept, the oldest dropped
    assert out.count(big) <= 1


def test_derive_title_is_first_line_truncated() -> None:
    assert derive_title("Plan my trip\nto Rome") == "Plan my trip"
    assert len(derive_title("z" * 200)) == 80
    assert derive_title("   ") == "New chat"
