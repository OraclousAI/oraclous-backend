"""The driver failures upper layers may need to name (core layer — driver-facing plumbing).

``repositories/`` owns every SQL statement, and the service architecture standard (§3.5) keeps the
ORM out of ``services/``. But a caller that has to *react* to a constraint failure — retry an insert
with a different candidate value — needs the exception type, not a driver. Naming it once here, in
the layer that already owns the engine and the session, gives the layers above a handle without an
ORM import outside ``repositories/``/``core/``.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError as ConstraintViolation

__all__ = ["ConstraintViolation"]
