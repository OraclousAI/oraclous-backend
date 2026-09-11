"""#826 — the ``engine_provenance`` row model gains three additive, nullable columns.

Per the CTO's 11 September 2026 ruling on #826, the widened ``ProvenanceRecord`` fields
(``context``, ``input_hash``, ``output_hash``) are RETURNED on read, not merely stored — so the
ORM row itself must carry them before ``ActivityEvent``/the read repository can surface them.
Nullable: a lifecycle event (no input/output) has nothing to attest. A plain unit test (no DB) —
SQLAlchemy's mapped ``__table__.columns`` exist purely from the class definition.
"""

from __future__ import annotations

import pytest
from oraclous_execution_engine_service.models.provenance import EngineProvenanceEvent

pytestmark = pytest.mark.unit


def test_the_row_model_carries_the_three_new_nullable_columns() -> None:
    columns = EngineProvenanceEvent.__table__.columns
    for name in ("context", "input_hash", "output_hash"):
        assert name in columns, f"engine_provenance has no {name!r} column yet"
        assert columns[name].nullable, f"{name!r} must be nullable (a lifecycle event has neither)"


def test_the_five_original_columns_stay_required_and_unchanged() -> None:
    columns = EngineProvenanceEvent.__table__.columns
    for name in ("organisation_id", "principal", "action", "resource", "outcome"):
        assert name in columns
        assert not columns[name].nullable, f"{name!r} must stay required — no-break guarantee"
