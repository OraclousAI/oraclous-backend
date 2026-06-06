"""Organisation domain rules (ORAA-4 §21 domain layer) — slug + role hierarchy, no I/O.

Pure functions: ``slugify`` derives a URL handle from a name (the repository resolves collisions by
suffixing); ``OrgRole`` + ``role_rank``/``can_manage`` express the owner > admin > member hierarchy
that authorises org-management actions (threat T-PRIV).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 63


@dataclass(frozen=True)
class MemberView:
    """A read projection of an org membership joined with the member's email (no I/O)."""

    user_id: str
    email: str | None
    role: str
    since: datetime


class OrgRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


_RANK = {OrgRole.MEMBER: 0, OrgRole.ADMIN: 1, OrgRole.OWNER: 2}


def slugify(name: str) -> str:
    """Lowercase, hyphenate, trim to a valid slug. Falls back to ``org`` if nothing survives."""
    slug = _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")[:_SLUG_MAX].strip("-")
    return slug or "org"


def role_rank(role: str) -> int:
    """Numeric rank of a role; unknown roles rank below member (fail-closed)."""
    try:
        return _RANK[OrgRole(role)]
    except ValueError:
        return -1


def can_manage(actor_role: str, *, min_role: OrgRole = OrgRole.ADMIN) -> bool:
    """True if ``actor_role`` is at least ``min_role`` (owner ≥ admin ≥ member)."""
    return role_rank(actor_role) >= _RANK[min_role]
