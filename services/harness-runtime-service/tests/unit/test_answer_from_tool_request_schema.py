"""#900 (ADR-053 decision 2) — ``ExecuteHarnessRequest`` gains ``answer_from_tool``, the wire-shape
half of the ``requires_valid_json``/``on_exhaustion`` precedent (`test_fetched_urls_request_schema
.py` bounds the same DTO for the analogous #975 seed fields).

The dispatching member's declaration (a tool name whose terminating call constitutes its answer)
rides the engine→harness-runtime HTTP call the same "send-only-when-set" way
``requires_valid_json`` and ``on_exhaustion`` already do: an optional field, ``None``/absent
meaning no declaration, unchanged behaviour.

Today ``ExecuteHarnessRequest`` declares no such field, and carries no ``model_config`` forbidding
extra keys (``extra="ignore"``, pydantic's default) — so passing ``answer_from_tool`` raises nothing
at construction; the attribute simply does not exist afterward. Every assertion below therefore
fails on BEHAVIOUR (reading the field back), never merely a constructor ``TypeError`` — RED until
the [impl] declares the field.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]


def _request(**overrides: object):  # noqa: ANN202
    from oraclous_harness_runtime_service.schema.harness_schemas import ExecuteHarnessRequest

    base: dict[str, object] = {"manifest": {"ohm_version": "1.0"}, "input": "go"}
    base.update(overrides)
    return ExecuteHarnessRequest(**base)  # type: ignore[arg-type]


def test_answer_from_tool_accepts_a_tool_name_and_reads_back() -> None:
    req = _request(answer_from_tool="web.search")
    assert req.answer_from_tool == "web.search"  # AttributeError today: the field is not declared


def test_answer_from_tool_defaults_to_none_when_absent() -> None:
    req = _request()
    # Construction itself must never raise — the declaration is optional, same as
    # requires_valid_json/on_exhaustion. What it defaults TO is the behaviour under test.
    assert req.answer_from_tool is None  # AttributeError today: the field is not declared
