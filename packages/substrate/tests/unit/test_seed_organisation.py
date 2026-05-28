"""Seed organisation for single-org deployments (ORA-16 / A1, AC#5).

RED until `backend-implementer` adds `oraclous_substrate.organisation`.

A1 makes ``organisation_id`` mandatory on every substrate primitive. For a
single-organisation deployment to keep behaving as before, the substrate must
expose a stable, well-known seed organisation that such deployments scope
everything under transparently. These tests pin that the seed exists, is a
real UUID, is stable, and is usable as an ordinary ``organisation_id``.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit


def test_seed_organisation_id_is_a_uuid() -> None:
    from oraclous_substrate.organisation import SEED_ORGANISATION_ID

    assert isinstance(SEED_ORGANISATION_ID, uuid.UUID)


def test_seed_organisation_id_is_stable() -> None:
    """The seed must be a fixed constant — single-org rows scoped to it stay readable."""
    from oraclous_substrate.organisation import SEED_ORGANISATION_ID as first
    from oraclous_substrate.organisation import SEED_ORGANISATION_ID as second

    assert first == second


def test_seed_organisation_works_as_an_ordinary_org_scope() -> None:
    """A single-org deployment can build cache keys with the seed org without error."""
    from oraclous_substrate.cache_keys import query_cache_key
    from oraclous_substrate.organisation import SEED_ORGANISATION_ID

    key = query_cache_key(str(SEED_ORGANISATION_ID), "graph-abc", "hello", "graphrag")
    assert key.startswith(f"qcache:{SEED_ORGANISATION_ID}:")
