"""LLM rate table + pricing (#252): known-model USD math + unknown/None → unpriced (no fabrication).

Pure domain: no I/O. Asserts the per-Mtok math and the fail-closed UNPRICED contract for any model
absent from the static table.
"""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.billing.rates import RATES, price

pytestmark = pytest.mark.unit


def test_known_model_prices_per_mtok_math() -> None:
    # gpt-4o-mini = 0.15/Mtok input, 0.60/Mtok output. 2M input + 0.5M output:
    #   (2_000_000/1e6)*0.15 + (500_000/1e6)*0.60 = 0.30 + 0.30 = 0.60
    result = price("openrouter/openai/gpt-4o-mini", 2_000_000, 500_000)
    assert result.priced is True
    assert result.usd == pytest.approx(0.60)


def test_known_model_strips_provider_prefix() -> None:
    # the binding is <provider>/<openrouter-id>; pricing keys on the part after the FIRST '/'.
    a = price("openrouter/anthropic/claude-3.5-sonnet", 1_000_000, 1_000_000)
    b = price("openai/anthropic/claude-3.5-sonnet", 1_000_000, 1_000_000)
    # 3.00 + 15.00 = 18.00, regardless of the provider prefix.
    assert a.usd == pytest.approx(18.00)
    assert b.usd == pytest.approx(18.00)


def test_zero_tokens_known_model_is_zero_not_unpriced() -> None:
    result = price("openrouter/openai/gpt-4o", 0, 0)
    assert result.priced is True
    assert result.usd == pytest.approx(0.0)


def test_unknown_model_is_unpriced_no_fabrication() -> None:
    result = price("openrouter/acme/super-secret-model", 1_000_000, 1_000_000)
    assert result.priced is False
    assert result.usd is None


def test_none_model_is_unpriced() -> None:
    result = price(None, 500, 500)
    assert result.priced is False
    assert result.usd is None


def test_prefix_only_binding_is_unpriced() -> None:
    # a binding with no model id after the provider → no key → unpriced (never a guess).
    result = price("openrouter", 100, 100)
    assert result.priced is False
    assert result.usd is None


def test_table_seeds_the_documented_models() -> None:
    assert "openai/gpt-4o-mini" in RATES
    assert RATES["anthropic/claude-3.5-sonnet"] == {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
    }
    assert len(RATES) >= 8
