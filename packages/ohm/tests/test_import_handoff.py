"""## Handoff -> HandoffSpec (#408; ADR-034 §6)."""

from __future__ import annotations

from oraclous_ohm.import_.handoff import parse_handoff

_MULTI = """## Handoff
**Next agent**: <analyst | macro-strategist | user-decides>
**Next task**: "Refresh the regime label."
"""
_SINGLE = "**Next agent**: research-lead\n**Next task**: do the thing"


def test_multi_candidate_is_conditional() -> None:
    h = parse_handoff(_MULTI)
    assert h.next_agents == ["analyst", "macro-strategist"]  # user-decides dropped
    assert h.conditional is True
    assert "regime label" in h.next_task


def test_single_candidate_not_conditional() -> None:
    h = parse_handoff(_SINGLE)
    assert h.next_agents == ["research-lead"]
    assert h.conditional is False


def test_no_handoff_is_empty() -> None:
    h = parse_handoff("just a body, no handoff section")
    assert h.next_agents == []
    assert h.conditional is False
