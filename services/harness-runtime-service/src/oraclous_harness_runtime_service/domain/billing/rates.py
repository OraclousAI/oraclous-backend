"""LLM USD rate table + pricing — re-export shim (ADR-044 / #252).

The canonical price table + pure ``price()`` were relocated to the shared kernel
``oraclous_ohm.billing`` (#603) so BOTH independent Layer-3 services can price without importing
each other (the engine's cost pre-flight needs the same pure seam as this service's spend estimate).
This shim keeps the historical import path (``...domain.billing.rates``) working byte-for-byte, so
``spend_service`` and any external references are unchanged. Import from :mod:`oraclous_ohm.billing`
directly in new code.
"""

from __future__ import annotations

from oraclous_ohm.billing import (
    RATES,
    PriceResult,
    _model_id,
    price,
)

__all__ = ["RATES", "PriceResult", "_model_id", "price"]
