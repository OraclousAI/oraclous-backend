"""Unit tests for the organisation domain (slug + role hierarchy). No I/O."""

from __future__ import annotations

import pytest
from oraclous_auth_service.domain.organisations import (
    OrgRole,
    can_manage,
    role_rank,
    slugify,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Corp", "acme-corp"),
        ("  Hello, World!  ", "hello-world"),
        ("___", "org"),  # nothing survives -> fallback
        ("Über Café 99", "ber-caf-99"),
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert slugify(name) == expected


def test_slug_truncated_to_63() -> None:
    assert len(slugify("a" * 200)) == 63


def test_role_hierarchy() -> None:
    assert role_rank("owner") > role_rank("admin") > role_rank("member")
    assert role_rank("bogus") < role_rank("member")  # unknown ranks below member (fail-closed)


def test_can_manage_requires_admin_or_owner() -> None:
    assert can_manage("owner")
    assert can_manage("admin")
    assert not can_manage("member")
    assert can_manage("owner", min_role=OrgRole.OWNER)
    assert not can_manage("admin", min_role=OrgRole.OWNER)
