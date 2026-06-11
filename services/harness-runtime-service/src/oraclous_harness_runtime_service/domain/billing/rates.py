"""LLM USD rate table + pricing (ORAA-4 §21 pure-domain layer; #252).

ADR-009 stays intact: the substrate records RAW tokens only; this module is the READ-TIME pricing
layer that turns those tokens into an ESTIMATE of the user's provider spend (BYOM). It is NOT
platform billing.

IMPORTANT — these prices are estimates that DRIFT. They are seeded from public per-million-token
(``per_mtok``) USD list prices at authoring time; providers change them, and BYOM users may be on
different tiers. A model not in the table is returned UNPRICED (``priced=False``, ``usd=None``) —
its price is NEVER fabricated. The map is keyed by the OpenRouter-style model id, i.e. the part of
the OHM model binding AFTER the FIRST ``/`` (e.g. binding ``openrouter/openai/gpt-4o-mini`` →
``openai/gpt-4o-mini``).
"""

from __future__ import annotations

from dataclasses import dataclass

# model-id → {input_per_mtok, output_per_mtok} in USD per 1,000,000 tokens. Estimates; see module
# docstring. Output is priced higher than input (it costs ~3-4× more to generate).
RATES: dict[str, dict[str, float]] = {
    "openai/gpt-4o-mini": {"input_per_mtok": 0.15, "output_per_mtok": 0.60},
    "openai/gpt-4o": {"input_per_mtok": 2.50, "output_per_mtok": 10.00},
    "openai/gpt-4.1-mini": {"input_per_mtok": 0.40, "output_per_mtok": 1.60},
    "anthropic/claude-3.5-sonnet": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
    "anthropic/claude-3-haiku": {"input_per_mtok": 0.25, "output_per_mtok": 1.25},
    "google/gemini-1.5-flash": {"input_per_mtok": 0.075, "output_per_mtok": 0.30},
    "meta-llama/llama-3.1-8b-instruct": {"input_per_mtok": 0.05, "output_per_mtok": 0.08},
    "mistralai/mistral-small": {"input_per_mtok": 0.20, "output_per_mtok": 0.60},
}

_PER_MTOK = 1_000_000


@dataclass(frozen=True, slots=True)
class PriceResult:
    """The estimated USD spend for some input/output tokens. ``priced`` is False (and ``usd`` None)
    when the model is unknown / absent — such a model reports tokens only, never a guessed price."""

    usd: float | None
    priced: bool


def _model_id(model_binding: str | None) -> str | None:
    """Strip the OHM binding's provider prefix → the rate-table key. The binding is
    ``<provider>/<openrouter-model-id>`` (e.g. ``openrouter/openai/gpt-4o-mini``); the key is
    everything after the FIRST ``/`` (``openai/gpt-4o-mini``). ``None``/prefix-only → ``None``."""
    if not model_binding:
        return None
    _provider, sep, model_id = model_binding.partition("/")
    return model_id if sep and model_id else None


def price(model_binding: str | None, input_tokens: int, output_tokens: int) -> PriceResult:
    """Estimate the USD spend for ``input_tokens``/``output_tokens`` of the given model binding.

    Looks the model up in :data:`RATES` (after stripping the provider prefix). A known model returns
    ``priced=True`` with ``usd = (input/1e6)*in_rate + (output/1e6)*out_rate``; an unknown or
    ``None`` model returns ``priced=False``/``usd=None`` — the caller reports its tokens unpriced.
    """
    rate = RATES.get(_model_id(model_binding) or "")
    if rate is None:
        return PriceResult(usd=None, priced=False)
    usd = (input_tokens / _PER_MTOK) * rate["input_per_mtok"] + (output_tokens / _PER_MTOK) * rate[
        "output_per_mtok"
    ]
    return PriceResult(usd=usd, priced=True)
