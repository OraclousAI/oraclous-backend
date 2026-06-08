"""Declarative base + timestamp mixin (ORAA-4 §21 models layer).

Holds ORM *declarations* only (Mapped columns) — declaring schema is not driver/connection ACCESS,
which lives in repositories/ + core/ (STR004 exemption for the models/ layer). The gateway gains its
first database in R6 Slice 3 (the integration-key store, ADR-019).
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class BaseModel(Base):
    __abstract__ = True

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
