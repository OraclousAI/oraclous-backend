"""Models package — importing it registers every table on ``Base.metadata`` (for Alembic)."""

from __future__ import annotations

from oraclous_execution_engine_service.models.base_model import Base
from oraclous_execution_engine_service.models.job import EngineJob
from oraclous_execution_engine_service.models.provenance import EngineProvenanceEvent
from oraclous_execution_engine_service.models.roundtable import EngineRoundtable
from oraclous_execution_engine_service.models.schedule import EngineSchedule

__all__ = [
    "Base",
    "EngineJob",
    "EngineProvenanceEvent",
    "EngineRoundtable",
    "EngineSchedule",
]
