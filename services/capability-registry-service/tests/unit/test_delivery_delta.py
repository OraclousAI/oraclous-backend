"""Unit: the clean-delta / idempotency core Oraclous owns for deliver-back (#515, E6 / O7).

The canonical O7 proof — "a recurring refresh writes a clean diff, not a clobber" — is decided by
Oraclous BEFORE any forge call, so it is identical across github/gitea (API-agnostic): each file's
content is hashed; only files whose hash differs from the last delivery are written; and the whole
delivery collapses to a stable ``delivery_key`` so an identical re-deliver dedupes to a NO_OP
(``UNIQUE(organisation_id, delivery_key)`` on the ``delivery_state`` table — mirrors the engine_jobs
idempotency shape). Pure functions here; the persisted ``delivery_state`` is exercised in the
integration test.

RED until #515 [impl] lands ``oraclous_capability_registry_service.domain.delivery``. The seam is
imported FUNCTION-LOCALLY (§4.1) so collection stays green and only these tests fail at runtime.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_content_hash_is_stable_and_content_sensitive() -> None:
    from oraclous_capability_registry_service.domain.delivery import content_hash

    assert content_hash(b"hello") == content_hash(b"hello")  # deterministic
    assert content_hash(b"hello") != content_hash(b"world")  # collision-resistant
    assert len(content_hash(b"x")) == 64  # sha256 hex digest


def test_delivery_key_is_order_independent_and_scope_and_content_sensitive() -> None:
    from oraclous_capability_registry_service.domain.delivery import delivery_key

    base = delivery_key(
        organisation_id="org-1", repo="o/r", ref="deliver", file_hashes={"a.md": "h1", "b.md": "h2"}
    )
    # order-independent: the SAME file set in any order → the SAME key (a recurring refresh dedupes)
    assert base == delivery_key(
        organisation_id="org-1", repo="o/r", ref="deliver", file_hashes={"b.md": "h2", "a.md": "h1"}
    )
    # content-sensitive: a changed file → a different key (the delivery fires, not a NO_OP)
    assert base != delivery_key(
        organisation_id="org-1", repo="o/r", ref="deliver", file_hashes={"a.md": "h1", "b.md": "X"}
    )
    # scope-sensitive: org / repo / ref each change the key (no cross-tenant or cross-target dedup)
    assert base != delivery_key(
        organisation_id="org-2", repo="o/r", ref="deliver", file_hashes={"a.md": "h1", "b.md": "h2"}
    )
    assert base != delivery_key(
        organisation_id="org-1",
        repo="o/OTHER",
        ref="deliver",
        file_hashes={"a.md": "h1", "b.md": "h2"},
    )
    assert base != delivery_key(
        organisation_id="org-1", repo="o/r", ref="other", file_hashes={"a.md": "h1", "b.md": "h2"}
    )


def test_changed_paths_skips_unchanged_and_includes_new() -> None:
    from oraclous_capability_registry_service.domain.delivery import changed_paths

    stored = {"a.md": "h1", "b.md": "h2"}
    incoming = {"a.md": "h1", "b.md": "DIFF", "c.md": "h3"}  # a unchanged, b changed, c new
    assert set(changed_paths(incoming, stored)) == {
        "b.md",
        "c.md",
    }  # only the diff, never a clobber


def test_an_identical_delivery_yields_no_changed_paths() -> None:
    """The NO_OP signal: re-delivering the exact same content writes nothing (clean delta)."""
    from oraclous_capability_registry_service.domain.delivery import changed_paths

    files = {"a.md": "h1", "b.md": "h2"}
    assert changed_paths(files, files) == []


def test_a_first_delivery_writes_everything() -> None:
    from oraclous_capability_registry_service.domain.delivery import changed_paths

    incoming = {"a.md": "h1", "b.md": "h2"}
    assert set(changed_paths(incoming, {})) == {"a.md", "b.md"}  # nothing stored → all files new
