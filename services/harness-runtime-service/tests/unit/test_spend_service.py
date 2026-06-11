"""Spend read service (#252): per-model token sums + read-time pricing + org-scoping.

Unit: a fake execution repository stands in for the DB aggregation (the real SQL aggregation is
covered by the integration suite). Asserts the service prices known models, leaves unknown models
unpriced (tokens only), totals correctly, and ONLY ever queries the caller's org (scoping is the
repo's ``organisation_id`` filter — the service must never widen it).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_harness_runtime_service.repositories.execution_repository import ModelSpendRow
from oraclous_harness_runtime_service.services.spend_service import SpendService

pytestmark = pytest.mark.unit

_ORG_A = uuid.uuid4()
_ORG_B = uuid.uuid4()


class _FakeExecutions:
    """Returns per-org per-model rows, org-scoped — it only ever yields the rows for the org it is
    asked about, exactly like the real repository's mandatory ``organisation_id`` filter."""

    def __init__(self, rows_by_org: dict[uuid.UUID, list[ModelSpendRow]]) -> None:
        self._rows_by_org = rows_by_org
        self.queried_orgs: list[uuid.UUID] = []
        self.queried_since: datetime | None = None

    async def spend_by_model(self, organisation_id, *, since=None):  # noqa: ANN001, ANN202
        self.queried_orgs.append(organisation_id)
        self.queried_since = since
        return list(self._rows_by_org.get(organisation_id, []))


async def test_prices_known_model_and_leaves_unknown_unpriced() -> None:
    rows = {
        _ORG_A: [
            # priced: gpt-4o-mini = 0.15/Mtok in, 0.60/Mtok out. 1M in + 1M out = 0.75.
            ModelSpendRow(
                model="openrouter/openai/gpt-4o-mini",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                executions=3,
            ),
            # unknown model → unpriced (tokens only).
            ModelSpendRow(
                model="openrouter/acme/mystery",
                input_tokens=2_000,
                output_tokens=500,
                executions=1,
            ),
        ]
    }
    fake = _FakeExecutions(rows)
    out = await SpendService(executions=fake).estimate(_ORG_A)

    assert out.currency == "USD"
    by_model = {m.model: m for m in out.by_model}

    priced = by_model["openrouter/openai/gpt-4o-mini"]
    assert priced.priced is True
    assert priced.input_tokens == 1_000_000 and priced.output_tokens == 1_000_000
    assert priced.executions == 3
    assert priced.estimated_usd == pytest.approx(0.75)

    unknown = by_model["openrouter/acme/mystery"]
    assert unknown.priced is False
    assert unknown.estimated_usd is None
    assert unknown.input_tokens == 2_000 and unknown.output_tokens == 500

    # totals: USD sums only the priced row; tokens sum every row.
    assert out.total_estimated_usd == pytest.approx(0.75)
    assert out.total_input_tokens == 1_002_000
    assert out.total_output_tokens == 1_000_500
    assert out.unpriced_models == ["openrouter/acme/mystery"]


async def test_org_scoping_excludes_other_orgs() -> None:
    rows = {
        _ORG_A: [
            ModelSpendRow(
                model="openrouter/openai/gpt-4o-mini",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                executions=2,
            )
        ],
        _ORG_B: [
            ModelSpendRow(
                model="openrouter/openai/gpt-4o",
                input_tokens=5_000_000,
                output_tokens=9_000_000,
                executions=4,
            )
        ],
    }
    fake = _FakeExecutions(rows)

    out_a = await SpendService(executions=fake).estimate(_ORG_A)
    # only org A's single model is present; org B's gpt-4o spend is NOT visible.
    assert [m.model for m in out_a.by_model] == ["openrouter/openai/gpt-4o-mini"]
    assert out_a.total_input_tokens == 1_000_000
    assert out_a.total_estimated_usd == pytest.approx(0.75)
    # the repo was queried with org A only — the service never widens the scope.
    assert fake.queried_orgs == [_ORG_A]


async def test_since_window_is_passed_through() -> None:
    fake = _FakeExecutions({_ORG_A: []})
    since = datetime(2026, 1, 1, tzinfo=UTC)
    out = await SpendService(executions=fake).estimate(_ORG_A, since=since)
    assert fake.queried_since == since
    assert out.since == since
    assert out.by_model == []
    assert out.total_estimated_usd == 0.0
    assert out.total_input_tokens == 0 and out.total_output_tokens == 0
    assert out.unpriced_models == []


async def test_fake_mode_null_model_is_unpriced_not_listed() -> None:
    # a fake-mode run records model=None; it is unpriced but must NOT appear in unpriced_models
    # (that list is for real, named-but-unknown models).
    rows = {
        _ORG_A: [
            ModelSpendRow(model=None, input_tokens=0, output_tokens=0, executions=5),
        ]
    }
    out = await SpendService(executions=_FakeExecutions(rows)).estimate(_ORG_A)
    assert len(out.by_model) == 1
    assert out.by_model[0].model is None
    assert out.by_model[0].priced is False
    assert out.unpriced_models == []
    assert out.total_estimated_usd == 0.0
