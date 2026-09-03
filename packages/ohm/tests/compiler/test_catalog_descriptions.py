"""#713 — the drafter chooses tools from a menu that says what each tool does.

Compiler run ``a3443e24`` gave ``knowledge-retriever`` to the member whose job was "analyze the
changes and generate a review comment". That tool reads the org's indexed knowledge graph and knows
nothing about an unmerged pull request, so the member finished ``partial`` in every run. The gate
cannot catch this and should not try: ``knowledge-retriever`` is a registered, active, first-party
tool, so ADR-032 capability-absence has nothing to say. Fit-for-purpose is a different question.

The catalog was a list of slugs. Every descriptor row already carries
``descriptor.metadata.description``, so the drafter was choosing from a menu with no dish
descriptions.

WHERE the descriptions have to land is the non-obvious part. The drafter does not read the
surveyor's SUB-GOAL, it reads the surveyor's OUTPUT — and ``SURVEYOR_PROMPT`` has it echo
``{"name", "ref"}`` only. Descriptions put on the surveyor would have to survive a model retyping
them, which is the relay #705 already found unreliable (a model dropped names while re-typing a
72-entry list). So they are baked into the DRAFTER's own sub-goal, deterministically, the same way
the governance seed is.

RED-by-design until the ``[impl]`` lands: ``build_compiler_team`` takes no ``catalog_descriptions``
argument yet, so these fail at runtime on the unexpected keyword.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.compiler.team import build_compiler_team

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")

_DESCRIBED = [
    {
        "name": "github-mcp-pull-request-read",
        "description": "Read a pull request: its diff, files, commits and review comments.",
    },
    {
        "name": "knowledge-retriever",
        "description": "Search the organisation's indexed knowledge graph for stored documents.",
    },
    {"name": "web-research"},  # a seed tool: registered, but carries no description
]


def _drafter_subgoal(**kw: object) -> str:
    manifest, _subs = build_compiler_team(_ORG, **kw)  # type: ignore[arg-type]
    subgoal = {m.role: m for m in manifest.members}["manifest-drafter"].subgoal
    assert subgoal is not None
    return subgoal


def test_the_drafter_sees_what_each_tool_does() -> None:
    subgoal = _drafter_subgoal(
        objective="Review a GitHub pull request and post the review as a comment.",
        catalog_descriptions=_DESCRIBED,
    )
    assert "Read a pull request" in subgoal
    assert "indexed knowledge graph" in subgoal  # the tool that did not fit says so itself
    assert "github-mcp-pull-request-read" in subgoal


def test_a_tool_with_no_description_renders_as_its_name_alone() -> None:
    """``survey_catalog`` unions the seed inventory (bare slugs) with the live registry rows, so a
    tool with no description is normal. Render the name; never invent text for it."""
    subgoal = _drafter_subgoal(catalog_descriptions=[{"name": "web-research"}])
    assert "web-research" in subgoal
    assert '"description": ""' not in subgoal  # not an empty field either — just the name


def test_the_descriptions_are_bounded_so_the_prompt_stays_bounded() -> None:
    """31 active tools on the local stack, each description capped at 500 chars at import — 15k
    characters of prompt on every compile if it rides unbounded. Bound it here."""
    from oraclous_ohm.compiler.team import _DESCRIPTION_CHARS

    assert _DESCRIPTION_CHARS <= 500
    long_description = "x" * 900
    subgoal = _drafter_subgoal(
        catalog_descriptions=[{"name": "verbose-tool", "description": long_description}],
    )
    assert "x" * _DESCRIPTION_CHARS in subgoal
    assert "x" * (_DESCRIPTION_CHARS + 1) not in subgoal


def test_the_drafter_still_carries_the_governance_seed() -> None:
    """The descriptions are additive to the drafter's sub-goal — #596's governed-by-default seed
    must still be in there, or every compiled team ships ungoverned."""
    from oraclous_ohm.seeds import DEFAULT_POLICY_SET_REF

    subgoal = _drafter_subgoal(catalog_descriptions=_DESCRIBED)
    assert DEFAULT_POLICY_SET_REF in subgoal
    assert "max_tokens_per_member" in subgoal


def test_no_descriptions_leaves_the_drafter_subgoal_as_it_was() -> None:
    """A caller that passes none (the unit path, or a registry outage degrading to seed-only) gets
    byte-identical behaviour to today."""
    assert _drafter_subgoal() == _drafter_subgoal(catalog_descriptions=None)
