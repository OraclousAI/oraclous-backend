"""Unit tests for the organisation domain (slug + role hierarchy). No I/O."""

from __future__ import annotations

import pytest
from oraclous_auth_service.domain.organisations import (
    OrgRole,
    can_manage,
    default_org_name,
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


# --- default-org naming (#317) ------------------------------------------------
@pytest.mark.parametrize(
    ("full_name", "email", "expected"),
    [
        # first whitespace-delimited token of full_name
        ("Reza Test", "reza@ex.com", "Reza's Second Mind"),
        ("Reza", "reza@ex.com", "Reza's Second Mind"),
        # leading/trailing/internal whitespace is ignored (str.split with no arg)
        ("   Reza   Test  ", "reza@ex.com", "Reza's Second Mind"),
        ("\tReza\nTest", "reza@ex.com", "Reza's Second Mind"),
        # unicode names survive verbatim in the name (slugify, not this, handles the URL handle)
        ("Über Mensch", "u@ex.com", "Über's Second Mind"),
        # fallback to the email local-part when full_name is missing / empty / whitespace-only
        (None, "space1781260604882@oraclous.dev", "space1781260604882's Second Mind"),
        ("", "alice@ex.com", "alice's Second Mind"),
        ("   ", "bob@ex.com", "bob's Second Mind"),
    ],
)
def test_default_org_name(full_name: str | None, email: str, expected: str) -> None:
    assert default_org_name(full_name=full_name, email=email) == expected


def test_default_org_name_slug_derives_from_chosen_name() -> None:
    # the slug is derived from the returned name by the caller via slugify (uniqueness suffixing is
    # the repository's job) — proving the end-to-end name->slug shape the issue specifies.
    from_name = slugify(default_org_name(full_name="Reza Test", email="reza@ex.com"))
    assert from_name == "reza-s-second-mind"
    from_fallback = slugify(default_org_name(full_name=None, email="alice@ex.com"))
    assert from_fallback == "alice-s-second-mind"
