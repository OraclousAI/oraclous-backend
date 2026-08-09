"""The shared minting function — one function, over a ``SourceRef`` (#742, §CITE rev2/rev3).

§CITE: a citation is minted **once, at the tool-execution boundary**, and the three paths — a
connector read that goes on to ingest, a connector read used directly in a run, and a live web or
MCP read — **share one minting function**. So minting is a general function over a ``SourceRef``,
not a helper wired into the ingest pipeline. #742 delivers it and wires the ingest path only; #746
consumes it unchanged for the live-web / MCP boundary. Building it ingest-shaped would force a
rewrite there, which is why the generality is asserted directly rather than left to taste.

Every import of ``oraclous_citation`` is **function-local** (TST001); the tests hard-fail RED with
``ModuleNotFoundError`` until the paired ``[impl]`` lands, and never skip.

RED until backend-implementer creates ``oraclous_citation``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.unit

_WEB_BODY = "The article body, exactly as it was read.\n"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestMintingIsGeneralOverASourceRef:
    """Criterion 11: the minting function accepts any ``SourceRef``, not only an ingest-shaped
    one. No graph, no ingest job, no writer is in sight here — that is the point."""

    def test_mints_a_complete_citation_from_a_bare_source_ref(self) -> None:
        from oraclous_citation import SourceRef, compute_citation_id, mint_citation  # RED

        source = SourceRef(
            source_system="web-research",
            source_id="https://blog.example.com/posts/2026/partner-agreements",
            url="https://blog.example.com/posts/2026/partner-agreements",
            title="How partner agreements work",
        )

        citation = mint_citation(source, content=_WEB_BODY)

        assert citation.source_system == "web-research"
        assert citation.source_id == "https://blog.example.com/posts/2026/partner-agreements"
        assert citation.citation_id == compute_citation_id(
            "web-research",
            "https://blog.example.com/posts/2026/partner-agreements",
            _content_hash(_WEB_BODY),
        )

    def test_minting_the_same_source_twice_yields_the_same_id(self) -> None:
        from oraclous_citation import SourceRef, mint_citation  # RED

        source = SourceRef(
            source_system="github",
            source_id="o/r:a.md",
            revision="abc123",
            revision_kind="blob_sha",
        )
        assert mint_citation(source).citation_id == mint_citation(source).citation_id

    def test_retrieved_at_is_server_computed_and_outside_the_identity(self) -> None:
        """Two reads of the same unchanged revision are the same record, minutes apart."""
        from oraclous_citation import SourceRef, mint_citation  # RED

        source = SourceRef(
            source_system="github",
            source_id="o/r:a.md",
            revision="abc123",
            revision_kind="blob_sha",
        )
        morning = mint_citation(source, retrieved_at=datetime(2026, 8, 8, 9, 0, tzinfo=UTC))
        evening = mint_citation(source, retrieved_at=datetime(2026, 8, 8, 19, 0, tzinfo=UTC))

        assert morning.retrieved_at != evening.retrieved_at
        assert morning.citation_id == evening.citation_id

    def test_retrieved_at_defaults_to_now_when_the_caller_gives_none(self) -> None:
        from oraclous_citation import SourceRef, mint_citation  # RED

        before = datetime.now(UTC)
        citation = mint_citation(
            SourceRef(
                source_system="github",
                source_id="o/r:a.md",
                revision="abc123",
                revision_kind="blob_sha",
            )
        )
        after = datetime.now(UTC)
        assert before <= citation.retrieved_at <= after


class TestRevisionIsNeverNull:
    """Criterion 8 / §CITE: a global rule, not an ingest-only one. The platform always holds the
    content at the moment it mints, so it can always fall back to hashing it."""

    def test_a_source_native_revision_is_used_as_given(self) -> None:
        from oraclous_citation import SourceRef, mint_citation  # RED

        citation = mint_citation(
            SourceRef(
                source_system="github",
                source_id="o/r:a.md",
                revision="9f2a4c81b3d7e5061c8a4f2b6d90e37c15ab8e42",
                revision_kind="blob_sha",
            ),
            content="README contents that must NOT override the blob sha.\n",
        )
        assert citation.revision == "9f2a4c81b3d7e5061c8a4f2b6d90e37c15ab8e42"
        assert citation.revision_kind == "blob_sha"

    def test_an_absent_revision_falls_back_to_the_content_sha256(self) -> None:
        from oraclous_citation import SourceRef, mint_citation  # RED

        citation = mint_citation(
            SourceRef(source_system="web-research", source_id="https://x/y"),
            content=_WEB_BODY,
        )
        assert citation.revision == _content_hash(_WEB_BODY)
        assert citation.revision_kind == "content_hash"

    def test_changed_content_with_no_inbound_revision_supersedes(self) -> None:
        """The supersession mechanism for a source that exposes no version of its own."""
        from oraclous_citation import SourceRef, mint_citation  # RED

        source = SourceRef(source_system="web-research", source_id="https://x/y")
        first = mint_citation(source, content="Revenue target is 4M.\n")
        second = mint_citation(source, content="Revenue target is 5M.\n")
        assert first.citation_id != second.citation_id

    def test_minting_fails_closed_when_neither_a_revision_nor_content_is_available(self) -> None:
        """A half-citation must be impossible to produce. Silently minting with an empty or
        invented revision would forge exactly the provenance this Contract exists to prevent."""
        from oraclous_citation import SourceRef, mint_citation  # RED

        with pytest.raises(ValueError):
            mint_citation(SourceRef(source_system="web-research", source_id="https://x/y"))


class TestASourceRefCannotCarryPlatformComputedFields:
    """The inbound boundary. The platform mints ``citation_id`` and ``retrieved_at``; a caller
    that supplies either is asserting provenance it was never issued."""

    def test_source_ref_rejects_a_caller_supplied_citation_id(self) -> None:
        from oraclous_citation import SourceRef  # RED
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SourceRef(
                source_system="github",
                source_id="o/r:a.md",
                citation_id="cit_" + "0" * 32,  # type: ignore[call-arg]
            )

    def test_source_ref_rejects_a_revision_without_its_kind(self) -> None:
        """A version with no statement of what kind it is cannot be told apart from our own
        content-hash fallback, which is the one job ``revision_kind`` has."""
        from oraclous_citation import SourceRef  # RED
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SourceRef(source_system="github", source_id="o/r:a.md", revision="abc123")


class TestACitationIsNeverPartlyFilled:
    """A record either carries a complete citation or ``null``. There is no middle state, and
    the model is where that is made unrepresentable."""

    @pytest.mark.parametrize(
        "missing",
        [
            "citation_id",
            "source_system",
            "source_id",
            "revision",
            "revision_kind",
            "url",
            "title",
            "author",
            "retrieved_at",
            "permission_ref",
        ],
    )
    def test_a_citation_missing_any_field_cannot_be_constructed(self, missing: str) -> None:
        from oraclous_citation import Citation  # RED
        from pydantic import ValidationError

        complete = {
            "citation_id": "cit_" + "0" * 32,
            "source_system": "github",
            "source_id": "o/r:a.md",
            "revision": "abc123",
            "revision_kind": "blob_sha",
            "url": "https://github.com/o/r/blob/abc123/a.md",
            "title": "a.md",
            "author": None,
            "retrieved_at": "2026-08-08T09:14:22Z",
            "permission_ref": None,
        }
        del complete[missing]

        with pytest.raises(ValidationError):
            Citation(**complete)  # type: ignore[arg-type]

    def test_a_citation_rejects_a_null_revision(self) -> None:
        from oraclous_citation import Citation  # RED
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Citation(
                citation_id="cit_" + "0" * 32,
                source_system="web-research",
                source_id="https://x/y",
                revision=None,  # type: ignore[arg-type]
                revision_kind="content_hash",
                url="https://x/y",
                title="y",
                author=None,
                retrieved_at="2026-08-08T09:14:22Z",
                permission_ref=None,
            )
