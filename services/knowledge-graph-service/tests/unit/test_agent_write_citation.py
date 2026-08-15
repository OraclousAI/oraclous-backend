"""A member's own writing is minted as ``agent``, not as an upload (#786; §CITE rev4 decision 7).

The defect, as it stands on ``main``. ``routes/internal_routes.py`` is the agent-write door: it
accepts a ``producer_kind`` of ``team-member`` or ``standalone-agent`` from the connector's instance
configuration — the trusted path, never the model — and forwards the request's ``source``. An agent
writing its own summary has no external source, so ``source`` is ``None``, and the ingest minting
path falls through to the upload branch. The record, its citation, and every surface that reads them
then say a person uploaded a document that a member in fact wrote.

Why that matters more than a label. An agent can write a file, have a later member retrieve it, and
cite it — passing every §CITE rule while showing the reader something that looks like external
evidence and is the agent quoting itself. Decision 7 makes the two legible apart.

The seam. ``IngestionService.ingest`` mints the citation (``_mint_for_ingest``) and today is never
told who produced the write, although ``IngestionPayload.producer_kind`` already carries it and
``tasks/ingest_tasks.py`` already reads it for document keying (``_AGENT_PRODUCERS``). These tests
drive ``ingest`` with a ``producer_kind`` keyword. **The name is the test-author's proposal, not a
ruling** — an implementer who would rather pass the whole ``ProducerRef``, or mint a step earlier,
should say so on the ``[impl]`` PR rather than bend the pipeline to this signature.

Two layers, on purpose. The first classes drive ``IngestionService`` directly, which is where the
minting decision is made and is the cheapest place to pin every branch of it. The last class drives
the real worker over a stored job row, because that is the only channel between the job the route
wrote and the code that mints — and an implementation can satisfy every service-level test while
never wiring the two together. That gap was found at the Tests Review gate with a throwaway patch
that turned the service tests green and changed nothing on a running stack.

RED until backend-implementer mints the agent value on the agent-write path.
"""

from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

# ``oraclous_citation`` and its ``SourceRef`` are already built, so they import normally. Only
# ``AGENT_SOURCE_SYSTEM`` is the missing seam, and it is asserted in this package's own suite.
from oraclous_citation import SourceRef
from oraclous_knowledge_graph_service.domain.job import IngestionPayload
from oraclous_knowledge_graph_service.repositories.graph_write_repository import WriteResult
from oraclous_knowledge_graph_service.services.embedder import HashingEmbedder
from oraclous_knowledge_graph_service.services.ingestion_service import IngestionService
from oraclous_knowledge_graph_service.tasks import ingest_tasks

pytestmark = pytest.mark.unit

_JOB_ID = "3f6b1d92-7c48-4a10-9e33-5b2c0d81af64"
_WRITTEN = b"The summary the member wrote.\n\nA second paragraph, so the chunker has work."
_GITHUB_SOURCE = SourceRef.model_validate(
    {
        "source_system": "github",
        "source_id": "OraclousAI/oraclous-backend:README.md",
        "revision": "9f2a4c81b3d7e5061c8a4f2b6d90e37c15ab8e42",
        "revision_kind": "blob_sha",
        "url": "https://github.com/OraclousAI/oraclous-backend/blob/9f2a4c81/README.md",
        "title": "README.md",
        "author": None,
        "permission_ref": None,
    }
)

#: Distinguishes "this test does not pass a producer at all" — the pre-#786 signature, which must
#: keep working — from "this test passes ``None``". Without it every test in the file would go RED
#: on the missing keyword, and the guards below would prove nothing about today's behaviour.
_NO_PRODUCER_ARGUMENT = object()


class _CapturingWriteRepo:
    """Captures the citation the service stamps, which is the whole assertion surface here."""

    def __init__(self) -> None:
        self.citation = None

    async def write_document(
        self,
        *,
        graph_id,
        document,
        chunks,
        embeddings,
        title=None,
        entity_graph=None,
        ontology_violations=0,
        ontology_coercions=0,
        citation=None,
    ):
        self.citation = citation
        return WriteResult(nodes=1 + len(chunks), relationships=len(chunks), chunks=len(chunks))


async def _stamped(*, producer_kind=_NO_PRODUCER_ARGUMENT, source: SourceRef | None = None):
    """Run one ingest and return the citation it stamped."""
    repo = _CapturingWriteRepo()
    service = IngestionService(repo, HashingEmbedder(dim=8))
    extra = {} if producer_kind is _NO_PRODUCER_ARGUMENT else {"producer_kind": producer_kind}
    await service.ingest(
        graph_id="g1",
        document="summary.md",
        data=_WRITTEN,
        source_type="text",
        source=source,
        job_id=_JOB_ID,
        **extra,
    )
    return repo.citation


class TestAgentWrittenContentIsMintedAsAgent:
    """Both agent producer kinds reach the same door, so both are asserted."""

    @pytest.mark.parametrize("producer_kind", ["team-member", "standalone-agent"])
    async def test_an_agent_write_with_no_external_source_is_not_an_upload(
        self, producer_kind: str
    ) -> None:
        citation = await _stamped(producer_kind=producer_kind)

        assert citation is not None
        assert citation.source_system == "agent"

    async def test_the_agent_citation_resolves_to_the_ingest_job(self) -> None:
        """§CITE's fourth minting row: ``source_id`` is the ingest job id, ``revision`` the content
        hash, ``url`` null. There is no external source to point at, so the record is legible
        rather than resolvable."""
        citation = await _stamped(producer_kind="team-member")

        assert citation.source_id == _JOB_ID
        assert citation.revision_kind == "content_hash"
        assert citation.url is None


class TestWhatMustNotChange:
    """Decision 7 turns on who authored the content, so it must not reach past that.

    The first two are **green today** and stay green: they are the guard against an implementation
    that over-reaches, and they are the reason this file does not simply assert the new branch.
    """

    async def test_a_person_uploading_a_document_still_uploads(self) -> None:
        """A human upload is a different act and keeps ``upload`` — the distinction decision 7
        draws is authorship, not the absence of a connector."""
        citation = await _stamped()

        assert citation.source_system == "upload"

    async def test_a_connector_ingest_still_carries_the_connector(self) -> None:
        """The path #800 and #742 already built. Nothing here may disturb it."""
        citation = await _stamped(source=_GITHUB_SOURCE)

        assert citation.source_system == "github"
        assert citation.revision_kind == "blob_sha"

    async def test_an_agent_write_that_carries_a_real_source_keeps_it(self) -> None:
        """A member that reads a real GitHub file and writes it in has a real origin. Overwriting
        that with ``agent`` would destroy provenance in the name of recording it."""
        citation = await _stamped(producer_kind="team-member", source=_GITHUB_SOURCE)

        assert citation.source_system == "github"
        assert citation.revision_kind == "blob_sha"

    async def test_an_unrecognised_producer_falls_back_to_upload(self) -> None:
        """Fail-safe in the same direction the document-keying gate already chose: anything not a
        known agent kind reads as the old behaviour, never as a silent agent write."""
        citation = await _stamped(producer_kind="something-we-do-not-know")

        assert citation.source_system == "upload"


# --- The producer actually reaching the minting service -----------------------
#
# Everything above drives ``IngestionService`` directly, which leaves one hole: nothing proves the
# producer the job row carries ever REACHES that service. An implementation can satisfy every test
# above and change nothing on a running stack, because the worker is the only channel between the
# stored job and the code that mints. `be-test-reviewer` demonstrated exactly that with a throwaway
# patch, and asked for this test rather than trusting the deployed-stack proof to catch it.
#
# So this drives the real ``_ingest_async`` over a stored payload and asserts the citation that
# lands at the writer. It deliberately does NOT patch ``IngestionService`` or assert a call
# signature: the producer may reach minting as a keyword, as a ``ProducerRef``, or by some other
# route, and only the stamped citation is the behaviour anyone depends on. Substrate, credential,
# extractor and Redis are patched, following ``test_ingest_task_retry.py``.

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")


class _CapturingWriterFactory:
    """Stands in for ``GraphWriteRepository`` and keeps the citation the real service stamps."""

    def __init__(self) -> None:
        self.repo = _CapturingWriteRepo()

    def __call__(self, _driver, database=None):  # noqa: ARG002
        return self.repo


def _payload(*, producer_kind: str | None) -> IngestionPayload:
    return IngestionPayload(
        graph_id=uuid.uuid4(),
        source_type="text",
        filename="summary.md",
        source_content=base64.b64encode(_WRITTEN).decode("ascii"),
        recipe_id=None,
        valid_from=None,
        valid_to=None,
        event_time=None,
        producer_kind=producer_kind,
        source=None,
    )


def _patch_the_worker(monkeypatch: pytest.MonkeyPatch, payload: IngestionPayload):
    """Patch everything the worker touches except the ingestion path under test."""

    @asynccontextmanager
    async def _session_cm():
        yield MagicMock(name="session", commit=AsyncMock())

    monkeypatch.setattr(ingest_tasks, "make_worker_engine", lambda: MagicMock(dispose=AsyncMock()))
    monkeypatch.setattr(
        ingest_tasks, "make_sessionmaker", lambda _engine: MagicMock(side_effect=_session_cm)
    )
    monkeypatch.setattr(
        ingest_tasks, "make_neo4j_driver", lambda _settings: MagicMock(close=MagicMock())
    )
    monkeypatch.setattr(
        ingest_tasks,
        "IngestionJobRepository",
        lambda _session: MagicMock(
            load_payload=AsyncMock(return_value=payload), update_status=AsyncMock()
        ),
    )
    monkeypatch.setattr(
        ingest_tasks,
        "GraphRepository",
        lambda _session: MagicMock(get_ontology=AsyncMock(return_value=None), get=AsyncMock()),
    )
    monkeypatch.setattr(ingest_tasks, "credential_for_graph", AsyncMock(return_value=None))
    monkeypatch.setattr(ingest_tasks.Ontology, "of", staticmethod(lambda _data: None))
    monkeypatch.setattr(ingest_tasks, "to_graph_schema", lambda _o: None)
    monkeypatch.setattr(ingest_tasks, "to_prompt_prefix", lambda _o: None)
    monkeypatch.setattr(ingest_tasks, "make_extractor", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(
        ingest_tasks,
        "make_embedder",
        lambda *a, **k: HashingEmbedder(dim=8),  # noqa: ARG005
    )
    monkeypatch.setattr(ingest_tasks.GraphGenerationRepository, "bump_for", lambda **_k: None)

    writer = _CapturingWriterFactory()
    monkeypatch.setattr(ingest_tasks, "GraphWriteRepository", writer)
    return writer


class TestTheProducerReachesTheMintingService:
    """The worker is the only channel from the stored job to the code that mints."""

    async def test_a_stored_agent_write_is_minted_as_agent_by_the_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writer = _patch_the_worker(monkeypatch, _payload(producer_kind="team-member"))

        await ingest_tasks._ingest_async(str(uuid.uuid4()), str(_ORG))

        assert writer.repo.citation is not None
        assert writer.repo.citation.source_system == "agent"

    async def test_a_stored_upload_is_still_minted_as_upload_by_the_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same path, proving the worker forwards what the row says rather than a constant."""
        writer = _patch_the_worker(monkeypatch, _payload(producer_kind=None))

        await ingest_tasks._ingest_async(str(uuid.uuid4()), str(_ORG))

        assert writer.repo.citation.source_system == "upload"
