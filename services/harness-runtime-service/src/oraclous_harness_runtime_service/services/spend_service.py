"""Spend read service (ORAA-4 §21 services layer; #252).

Assembles the spend ESTIMATE for an org: it asks the execution repository for per-model raw-token
sums (org-scoped — never cross-org), prices each priced row through the pure
``domain.billing.rates`` table, and totals the priced rows. ADR-009 stays intact — the substrate
stored raw tokens; pricing happens here, at read time, and the result is labelled an estimate of the
user's provider spend (BYOM), not platform billing. An unpriced model reports tokens only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from oraclous_harness_runtime_service.domain.billing.rates import price
from oraclous_harness_runtime_service.repositories.execution_repository import ExecutionRepository
from oraclous_harness_runtime_service.schema.harness_schemas import ModelSpendOut, SpendResponse


class SpendService:
    def __init__(self, executions: ExecutionRepository) -> None:
        self._executions = executions

    async def estimate(
        self, organisation_id: uuid.UUID, *, since: datetime | None = None
    ) -> SpendResponse:
        """Per-model + total spend estimate for the org's executions over an optional ``since``
        window. Priced rows carry ``estimated_usd``; unpriced rows (unknown model) report tokens
        only and are collected into ``unpriced_models``. Totals sum every row's tokens but only the
        priced rows' USD."""
        # A tz-naive `since` (a client that sent an ISO without an offset) is read as UTC, so the
        # comparison against the timezone-aware created_at is well-defined rather than ambiguous.
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        rows = await self._executions.spend_by_model(organisation_id, since=since)
        by_model: list[ModelSpendOut] = []
        total_usd = 0.0
        total_input = 0
        total_output = 0
        unpriced: list[str] = []
        for row in rows:
            result = price(row.model, row.input_tokens, row.output_tokens)
            if result.priced and result.usd is not None:
                total_usd += result.usd
            elif row.model is not None:
                unpriced.append(row.model)
            total_input += row.input_tokens
            total_output += row.output_tokens
            by_model.append(
                ModelSpendOut(
                    model=row.model,
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                    executions=row.executions,
                    estimated_usd=result.usd if result.priced else None,
                    priced=result.priced,
                )
            )
        return SpendResponse(
            since=since,
            currency="USD",
            by_model=by_model,
            total_estimated_usd=total_usd,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            unpriced_models=unpriced,
        )
