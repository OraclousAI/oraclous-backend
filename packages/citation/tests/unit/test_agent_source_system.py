"""``agent`` is a reserved ``source_system``, beside ``upload`` (#786; §CITE rev4 decision 7).

§CITE says ``source_system`` is a registered connector slug and **not** a frozen enum, plus exactly
two reserved values: ``upload`` for a file a person uploaded directly, and ``agent`` for content a
harness member wrote itself. Only the first exists today, so a file an agent writes into a
workspace is recorded as an upload — the case decision 7 exists to stop.

Why the value needs to be its own reserved constant rather than a literal at each call site: the
same string is read by the ingest minting path, by the artifact read surface, and by the console.
``UPLOAD_SOURCE_SYSTEM`` is already shared for that reason, and a second reserved value with a
different discipline would drift.

Every import of ``oraclous_citation`` is **function-local** (TST001); these tests hard-fail RED
with ``ImportError`` until the paired ``[impl]`` lands, and never skip.

RED until backend-implementer adds ``AGENT_SOURCE_SYSTEM`` to ``oraclous_citation``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_JOB_ID = "3f6b1d92-7c48-4a10-9e33-5b2c0d81af64"
_WRITTEN = "The summary the member wrote, in its own words.\n"


class TestTheReservedValue:
    """Decision 7 needs a value of its own, because the distinction is who authored the content."""

    def test_agent_is_exported_as_a_reserved_source_system(self) -> None:
        from oraclous_citation import AGENT_SOURCE_SYSTEM  # RED

        assert AGENT_SOURCE_SYSTEM == "agent"

    def test_agent_is_not_the_upload_value(self) -> None:
        """A human uploading a real document is a different act, so it keeps its own value."""
        from oraclous_citation import AGENT_SOURCE_SYSTEM, UPLOAD_SOURCE_SYSTEM  # RED

        assert AGENT_SOURCE_SYSTEM != UPLOAD_SOURCE_SYSTEM


class TestTheIdentityItProduces:
    """The reserved value is part of ``citation_id``, so the two acts can never collide."""

    def test_the_same_job_id_minted_as_agent_and_as_upload_are_two_records(self) -> None:
        """``citation_id`` hashes ``source_system`` first, so this must already hold — it is
        asserted because it is the property that makes decision 7 worth anything. If one job id
        could yield one id under both values, relabelling an agent write would silently rewrite
        the record a reader already saw."""
        from oraclous_citation import (  # RED
            AGENT_SOURCE_SYSTEM,
            UPLOAD_SOURCE_SYSTEM,
            SourceRef,
            mint_citation,
        )

        as_agent = mint_citation(
            SourceRef(source_system=AGENT_SOURCE_SYSTEM, source_id=_JOB_ID, url=None),
            content=_WRITTEN,
        )
        as_upload = mint_citation(
            SourceRef(source_system=UPLOAD_SOURCE_SYSTEM, source_id=_JOB_ID, url=None),
            content=_WRITTEN,
        )

        assert as_agent.citation_id != as_upload.citation_id

    def test_an_agent_citation_has_nothing_to_open(self) -> None:
        """§CITE: the agent row's ``url`` is null by construction — there is nothing outside the
        platform to open. The content hash carries the revision, because a member's own writing has
        no source-native version."""
        from oraclous_citation import AGENT_SOURCE_SYSTEM, SourceRef, mint_citation  # RED

        citation = mint_citation(
            SourceRef(source_system=AGENT_SOURCE_SYSTEM, source_id=_JOB_ID, url=None),
            content=_WRITTEN,
        )

        assert citation.url is None
        assert citation.revision_kind == "content_hash"
