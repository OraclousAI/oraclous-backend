"""Declarative base for the auth-service ORM models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base; all auth-service tables hang off this metadata."""
