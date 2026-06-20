"""core/evaluate (ADR-037 / #469) request schema — the flow-level evaluator's internal-plane input.

NOTE the org-scoping invariant (ADR-037 H2): the request carries NO ``organisation_id``. The graded
output's org is server-stamped from the authenticated principal in the route, never the body — so a
caller can never forge a foreign-org verdict.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FlowEvaluateRequest(BaseModel):
    """Grade ``target_output`` against ``success_criteria``. ``success_criteria`` is the manifest's
    prose criterion, or a ``battery:<name>`` reference (the named battery — #470)."""

    target_kind: Literal["run", "stage", "member_output"] = "member_output"
    target_ref: str = Field(min_length=1)  # an opaque id of what was graded (e.g. run/member id)
    target_output: str = Field(
        min_length=1
    )  # the inline output text to grade (no fetch-by-ref yet)
    success_criteria: str = Field(min_length=1)
    pass_threshold: float = 0.7
