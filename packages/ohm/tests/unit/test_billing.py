"""ADR-044 price table + pricing, now canonical in ``oraclous_ohm.billing`` (#603 relocation).

Pure domain: no I/O. Asserts the per-Mtok math, the fail-closed UNPRICED contract for any model
absent from the static table (never a fabricated price), and that the #603 dec-4(c) cheaper
scheduled-scan default is ITSELF priceable (else the cheaper default would price as unpriced,
defeating the pre-flight). The harness-runtime shim re-export is covered by its own test_rates.py.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.billing import RATES, SCHEDULED_SCAN_DEFAULT_BINDING, price

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
    assert a.usd == pytest.approx(18.00) and b.usd == pytest.approx(18.00)


def test_zero_tokens_known_model_is_zero_not_unpriced() -> None:
    result = price("openrouter/openai/gpt-4o", 0, 0)
    assert result.priced is True and result.usd == pytest.approx(0.0)


def test_unknown_model_is_unpriced_no_fabrication() -> None:
    result = price("openrouter/acme/super-secret-model", 1_000_000, 1_000_000)
    assert result.priced is False and result.usd is None


def test_none_and_prefix_only_bindings_are_unpriced() -> None:
    assert price(None, 500, 500).priced is False
    assert price("openrouter", 100, 100).priced is False  # no model id after the provider → no key


def test_table_seeds_the_documented_models() -> None:
    assert "openai/gpt-4o-mini" in RATES
    assert RATES["anthropic/claude-3.5-sonnet"]["output_per_mtok"] == 15.00
    assert len(RATES) >= 8


def test_scheduled_scan_default_binding_is_itself_priceable() -> None:
    # #603 dec-4(c): the cheaper scan default the fleet actually runs on MUST be priceable, or the
    # pre-flight would report the very default it applied as "unpriced".
    result = price(SCHEDULED_SCAN_DEFAULT_BINDING, 1_000, 1_000)
    assert result.priced is True and result.usd is not None
