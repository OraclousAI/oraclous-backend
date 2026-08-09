"""Ingest DTOs accept the connector's ``source`` (#742, criterion 5; §CITE rev3).

``IngestTextRequest``, ``BatchIngestItem`` and ``InternalIngestRequest`` each gain **one** optional
field, ``source: SourceRef | None = None``. The connector fills it, and only the connector; the
ingest caller passes it through **unmodified** and never synthesises one. A body without ``source``
stays valid — every existing caller, and every non-GitHub connector in this slice, keeps working
and produces ``citation: null``, which is the Contract's defined meaning for a record with no
source identity.

``organisation_id`` stays off the wire (ORG001) — ``source`` does not change that.

``oraclous_citation`` is imported **function-locally** (TST001); those tests hard-fail RED with
``ModuleNotFoundError`` until the paired ``[impl]`` lands, and never skip. The service's own schema
module is already built, so it is imported normally.

RED until backend-implementer adds ``source`` to the three ingest DTOs.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_knowledge_graph_service.schema.ingest_schemas import (
    BatchIngestItem,
    IngestTextRequest,
    InternalIngestRequest,
)

pytestmark = pytest.mark.unit

_SOURCE = {
    "source_system": "github",
    "source_id": "OraclousAI/oraclous-backend:README.md",
    "revision": "9f2a4c81b3d7e5061c8a4f2b6d90e37c15ab8e42",
    "revision_kind": "blob_sha",
    "url": "https://github.com/OraclousAI/oraclous-backend/blob/9f2a4c81/README.md",
    "title": "README.md",
    "author": None,
    "permission_ref": None,
}


class TestSourceIsAcceptedOnEveryIngestEntryPoint:
    """All three bodies land content, so all three must be able to say where it came from."""

    def test_ingest_text_request_accepts_source(self) -> None:
        request = IngestTextRequest(content="hello", source=_SOURCE)
        assert request.source.source_system == "github"
        assert request.source.revision_kind == "blob_sha"

    def test_batch_ingest_item_accepts_source(self) -> None:
        item = BatchIngestItem(path="docs/a.md", content="hello", source=_SOURCE)
        assert item.source.source_id == "OraclousAI/oraclous-backend:README.md"

    def test_internal_ingest_request_accepts_source(self) -> None:
        request = InternalIngestRequest(
            graph_id=uuid.uuid4(), source_content="hello", source=_SOURCE
        )
        assert request.source.url is not None

    def test_source_is_the_typed_source_ref_not_a_free_dict(self) -> None:
        """A free-form dict would let any shape through, and the whole Contract is the shape."""
        from oraclous_citation import SourceRef  # RED

        request = IngestTextRequest(content="hello", source=_SOURCE)
        assert isinstance(request.source, SourceRef)

    def test_a_source_ref_instance_is_accepted_directly(self) -> None:
        """The connector hands a ``SourceRef`` across; the caller passes it through unmodified."""
        from oraclous_citation import SourceRef  # RED

        source = SourceRef(source_system="upload", source_id="job-1")
        assert IngestTextRequest(content="hello", source=source).source == source


class TestSourceIsOptional:
    """A body without ``source`` is still valid — no migration, no forced backfill, and every
    connector that does not emit one yet keeps working."""

    def test_ingest_text_request_without_source_is_valid(self) -> None:
        assert IngestTextRequest(content="hello").source is None

    def test_batch_ingest_item_without_source_is_valid(self) -> None:
        assert BatchIngestItem(path="docs/a.md", content="hello").source is None

    def test_internal_ingest_request_without_source_is_valid(self) -> None:
        request = InternalIngestRequest(graph_id=uuid.uuid4(), source_content="hello")
        assert request.source is None

    def test_a_source_with_no_revision_is_accepted_inbound(self) -> None:
        """``revision``/``revision_kind`` are optional INBOUND — the platform falls back to the
        content hash. A connector that cannot name a version is not rejected at the boundary."""
        bare = {"source_system": "web-research", "source_id": "https://x/y"}
        request = IngestTextRequest(content="hello", source=bare)
        assert request.source.revision is None


class TestTheInboundBoundaryRefusesPlatformComputedProvenance:
    """The platform mints ``citation_id`` and ``retrieved_at``. A caller supplying either is
    claiming provenance the platform never issued — it must not survive the boundary."""

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    def test_a_caller_supplied_citation_id_on_source_is_rejected(self) -> None:
        from pydantic import ValidationError

        forged = {**_SOURCE, "citation_id": "cit_" + "0" * 32}
        with pytest.raises(ValidationError):
            IngestTextRequest(content="hello", source=forged)

    def test_a_caller_supplied_retrieved_at_on_source_is_rejected(self) -> None:
        from pydantic import ValidationError

        forged = {**_SOURCE, "retrieved_at": "1999-01-01T00:00:00Z"}
        with pytest.raises(ValidationError):
            IngestTextRequest(content="hello", source=forged)
