"""#975 (§CITE cite-by-reference), plan §4 / S3 — `ExecuteHarnessRequest` bounds for the two new
seeds: `prior_fetched_urls` (the engine's per-role contribution, threaded in by #975 slice T5) and
`person_supplied_text` (task text + rendered answers, ruling 4/6 — a citable registry seed, trusted
exactly as `input_text` is).

**The `person_supplied_text` cap.** The plan asks for "the tightest existing task/answer size cap
in the codebase, NAMED in the test". A repo-wide grep for a `max_length` bound on a task-shaped
text field (`grep -rn "max_length" services | grep -v test`) turns up exactly one:
`CreateCompilerRunRequest.objective` — execution-engine-service's own "prose objective" (its own
docstring's words) a run executes against — `Field(min_length=1, max_length=8000)`
(`services/execution-engine-service/src/oraclous_execution_engine_service/schema/
engine_schemas.py:870`). No field named `task` or `answers` anywhere in the codebase carries a
numeric cap at all — the engine's own `resolve_run_task`/`_render_answers` (`services/team_run.py`)
render unbounded strings. 8000 is therefore not an average of several candidates; it is the one
number that exists. §CITE's `person_supplied_text` reuses it rather than inventing a new one for a
field that carries the very text `objective` already bounds (a compiler-drafted team's task IS
this run's task).

Today `ExecuteHarnessRequest` declares neither field, and carries no `model_config` forbidding
extra keys (`extra="ignore"`, pydantic's default) — so passing them raises nothing at
construction; the attributes simply do not exist afterward. Every assertion below therefore fails
on BEHAVIOUR (reading the field back, or a `ValidationError` that never arrives), never only on a
constructor `TypeError` — RED until `[impl]` slice I3 declares both fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

pytestmark = [pytest.mark.unit]

# Named per the docstring above: CreateCompilerRunRequest.objective, the tightest existing
# task-shaped text bound in the repo.
_PERSON_SUPPLIED_TEXT_MAX = 8000

# The list-length cap plan §4 states directly for `prior_fetched_urls` — computed from the loop's
# own already-shipped registry cap, never hand-derived.


def _prior_fetched_urls_max() -> int:
    from oraclous_harness_runtime_service.domain.loop.tool_use import _MAX_FETCHED_URLS

    return _MAX_FETCHED_URLS


def _request(**overrides: object):  # noqa: ANN202
    from oraclous_harness_runtime_service.schema.harness_schemas import ExecuteHarnessRequest

    base: dict[str, object] = {"manifest": {"ohm_version": "1.0"}, "input": "go"}
    base.update(overrides)
    return ExecuteHarnessRequest(**base)  # type: ignore[arg-type]


# --- prior_fetched_urls: at most _MAX_FETCHED_URLS (2000) items --------------------------------


def test_prior_fetched_urls_accepts_up_to_the_cap_and_reads_back() -> None:
    cap = _prior_fetched_urls_max()
    urls = [f"https://example.com/{i}" for i in range(cap)]
    req = _request(prior_fetched_urls=urls)
    assert req.prior_fetched_urls == urls  # AttributeError today: the field is not declared


def test_prior_fetched_urls_rejects_one_item_over_the_cap() -> None:
    cap = _prior_fetched_urls_max()
    urls = [f"https://example.com/{i}" for i in range(cap + 1)]
    with pytest.raises(ValidationError):
        _request(prior_fetched_urls=urls)  # today: silently ignored, no ValidationError at all


# --- person_supplied_text: bounded at the named cap ---------------------------------------------


def test_person_supplied_text_accepts_up_to_the_named_cap_and_reads_back() -> None:
    text = "x" * _PERSON_SUPPLIED_TEXT_MAX
    req = _request(person_supplied_text=text)
    assert req.person_supplied_text == text  # AttributeError today: the field is not declared


def test_person_supplied_text_rejects_one_character_over_the_named_cap() -> None:
    text = "x" * (_PERSON_SUPPLIED_TEXT_MAX + 1)
    with pytest.raises(ValidationError):
        _request(person_supplied_text=text)  # today: silently ignored, no ValidationError at all


# --- both fields default to absent without error (S3: "Absent fields → empty seed / the §1
# standalone default") -----------------------------------------------------------------------


def test_both_fields_default_to_absent_without_error() -> None:
    req = _request()
    # Construction itself must never raise — both are optional seeds. What it defaults TO is the
    # behaviour under test: an empty/absent seed, never a validation failure.
    assert req.prior_fetched_urls in (None, [])  # AttributeError today: no such field
    assert req.person_supplied_text is None  # AttributeError today: no such field
