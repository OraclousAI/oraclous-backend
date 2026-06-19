"""The shared import flag (ADR-034 §7, flag-not-guess).

Every silent default or undecidable point in the importer surfaces as an ``ImportFlag`` for the O8
dry-run, never resolved silently. It lives here (not in any one adapter) so the agent mapper (#405)
and the charter adapter (#406) emit the SAME type, which the dry-run (#409) aggregates.
``blocking`` gates GO; ``confirm`` is surfaced for review; ``info`` records a forced choice.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

FlagSeverity = Literal["blocking", "confirm", "info"]


class ImportFlag(BaseModel):
    """A surfaced import decision/risk for the O8 dry-run — never a silent resolution."""

    model_config = ConfigDict(extra="ignore")

    code: str
    severity: FlagSeverity
    member_role: str  # the member/team the flag attaches to ("" for a team-level charter flag)
    message: str
