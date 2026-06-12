"""Unit tests for CommunityRepository (#303) with a fake Neo4j driver.

No real Neo4j here — a recording fake stands in for the driver so we can assert (a) the GDS-missing
failure mode is classified into the typed ``GdsUnavailableError`` (not a swallowed 500), (b) every
read/write carries the bound ``organisation_id`` (the live scope, never a caller arg — the cross-org
isolation control), and (c) the projection is always dropped. The real ``gds.louvain`` execution is
proven against a live Neo4j-GDS container in ``tests/integration/test_community_gds.py``.
"""

from __future__ import annotations

import uuid

import pytest
from neo4j.exceptions import ClientError
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import use_organisation_context
from oraclous_knowledge_graph_service.domain.community import (
    DetectionInProgress,
    GdsUnavailableError,
)
from oraclous_knowledge_graph_service.repositories.community_repository import (
    CommunityRepository,
    _is_gds_missing,
)

pytestmark = [pytest.mark.unit, pytest.mark.organization_isolation]

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _ctx(org: uuid.UUID = _ORG) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=org,
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


class _FakeDriver:
    """Records every ``execute_query`` call; returns canned rows keyed by a query substring."""

    def __init__(self, *, gds_missing: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._gds_missing = gds_missing

    def execute_query(self, query: str, database_=None, **params):  # noqa: ANN001, ANN003
        self.calls.append((query, params))
        if self._gds_missing and "gds." in query:
            raise ClientError(
                {
                    "code": "Neo.ClientError.Procedure.ProcedureNotFound",
                    "message": "There is no procedure with the name `gds.graph.project`",
                }
            )
        # gds.graph.project RETURN name / louvain stream / counts — return empty-ish rows.
        return [], None, None


def test_is_gds_missing_classifies_procedure_not_found() -> None:
    exc = ClientError(
        {
            "code": "Neo.ClientError.Procedure.ProcedureNotFound",
            "message": "There is no procedure with the name `gds.louvain.stream`",
        }
    )
    assert _is_gds_missing(exc) is True
    # A real runtime error (not a missing plugin) must NOT be misclassified.
    assert _is_gds_missing(ValueError("some unrelated failure")) is False


def test_detect_raises_typed_error_when_gds_absent() -> None:
    driver = _FakeDriver(gds_missing=True)
    repo = CommunityRepository(driver)
    with use_organisation_context(_ctx()):
        with pytest.raises(GdsUnavailableError):
            repo.detect(graph_id="g1")


def test_every_query_is_org_scoped() -> None:
    """The cross-org isolation control: every read/write binds the LIVE organisation_id from the
    context, never a caller argument — so a caller cannot reach another org's data."""
    driver = _FakeDriver()
    repo = CommunityRepository(driver)
    with use_organisation_context(_ctx()):
        repo.count_entities(graph_id="g1")
        repo.list_communities(graph_id="g1", level=None, min_entities=1)
        repo.status(graph_id="g1")
        repo.analytics(graph_id="g1")
    # Every query that scopes by org must pass the bound org id as a parameter.
    org_param_calls = [p for q, p in driver.calls if "organisation_id" in q]
    assert org_param_calls, "expected org-scoped queries"
    for params in org_param_calls:
        assert params.get("organisation_id") == str(_ORG)


def test_detect_drops_projection_once() -> None:
    driver = _FakeDriver()
    repo = CommunityRepository(driver)
    with use_organisation_context(_ctx()):
        repo.detect(graph_id="g1")
    drops = [q for q, _ in driver.calls if "gds.graph.drop" in q]
    # Exactly ONE projection (single dendrogram run, no per-resolution sweep), dropped in finally.
    assert len(drops) == 1
    # And the single Louvain call requests the native dendrogram.
    louvain = [q for q, _ in driver.calls if "gds.louvain.stream" in q]
    assert len(louvain) == 1
    assert "includeIntermediateCommunities" in louvain[0]


class _FakeLock:
    """A minimal in-memory stand-in for a sync ``redis.Redis`` lock (SET NX EX / GET / DELETE)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):  # noqa: ANN001, ARG002
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):  # noqa: ANN001
        return self.store.get(key)

    def delete(self, key):  # noqa: ANN001
        self.store.pop(key, None)


def test_detect_acquires_and_releases_the_per_graph_lock() -> None:
    lock = _FakeLock()
    driver = _FakeDriver()
    repo = CommunityRepository(driver, lock_client=lock)
    with use_organisation_context(_ctx()):
        repo.detect(graph_id="g1")
    # Lock taken during the run and released in finally — no key survives a completed detect.
    assert lock.store == {}


def test_concurrent_detect_on_same_graph_is_refused() -> None:
    lock = _FakeLock()
    driver = _FakeDriver()
    repo = CommunityRepository(driver, lock_client=lock)
    key = "kgs:community_detect:11111111-1111-1111-1111-111111111111:g1"
    lock.store[key] = "held-by-another-run"  # simulate a run already holding the lock
    with use_organisation_context(_ctx()):
        with pytest.raises(DetectionInProgress):
            repo.detect(graph_id="g1")
    # The clear was never issued — the destructive rebuild did not race the in-flight run.
    assert not any("DETACH DELETE" in q for q, _ in driver.calls)
    # And we did not steal the other run's lock.
    assert lock.store[key] == "held-by-another-run"
