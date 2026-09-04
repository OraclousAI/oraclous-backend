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


# ── #731: DRIFT ALARM, not a repoint ──────────────────────────────────────────────────────────────
#
# This is deliberately NOT the same shape as the other four #731 call sites.
# ``organisations.slugify`` feeds a UNIQUE-indexed database column and a public URL handle
# (``/org/<slug>``); the owner ruling (2026-08-24) is that #731 repoints its INTERNALS at the
# shared ``oraclous_ohm._slug.basic_slug`` while keeping the existing 63-char cap + ``"org"``
# fallback as a wrapper, specifically so this function's OUTPUT never changes. This test does not
# repoint anything — it pins that the output really does stay unchanged by asserting local
# ``slugify`` equals the shared primitive (capped and defaulted the same way) over a corpus, so any
# future drift is caught HERE, at a unit test, rather than surfacing as a broken unique index or a
# changed URL for an existing organisation. The existing ``test_slugify`` /
# ``test_slug_truncated_to_63`` cases above stay unmodified — this is additive.

_DRIFT_CORPUS = [
    "Acme Corp",
    "  Hello, World!  ",
    "___",
    "Über Café 99",
    "a" * 200,
    "",
    "@",
    "a--b--c" * 20,
    "\tTabbed\nName\n",
    "😈 Evil Org",
    # A repoint at ``tool_slug`` instead of ``basic_slug`` (the exact trap this PR names as
    # highest-stakes for this call site) is invisible to every case above: none contains ``/`` or a
    # non-bare ``@``, and `tool_slug` agrees with `basic_slug` on those. These two values pin the
    # two ways `tool_slug` diverges — a ``/`` triggers its `#594` ``ns--`` foreign-namespace marker,
    # and a non-empty `@`-suffix is dropped as a version instead of being slugged like normal text.
    "Acme/Corp",
    "Acme Corp@2.0",
]


@pytest.mark.parametrize("name", _DRIFT_CORPUS)
def test_organisation_slugify_tracks_the_shared_primitive_capped_and_defaulted(name: str) -> None:
    """RED until the [impl] adds ``oraclous_ohm._slug.basic_slug`` — function-local import per
    ``.claude/rules/tests-seam-imports.md``, this is a not-yet-built intra-repo seam AND a package
    auth-service does not declare as a dependency yet (the [impl] adds it to pyproject.toml)."""
    from oraclous_ohm._slug import basic_slug

    expected = basic_slug(name)[:63].strip("-") or "org"
    assert slugify(name) == expected
