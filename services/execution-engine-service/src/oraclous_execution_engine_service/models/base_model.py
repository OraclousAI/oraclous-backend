"""Declarative base + timestamp mixin (ORAA-4 §21 models layer).

Holds ORM *declarations* only (Mapped columns) — declaring schema is not driver/connection ACCESS,
which lives in repositories/ + core/ (STR004 exemption for the models/ layer).

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    """Shared declarative base; all execution-engine tables hang off this metadata."""


class BaseModel(Base):
    __abstract__ = True

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
