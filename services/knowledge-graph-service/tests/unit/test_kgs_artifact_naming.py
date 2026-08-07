"""#728 — an artifact is named for a person, and keyed so two members cannot collide.

Run ``dc167d8e`` landed 7 artifacts and kept 1: every write arrived as ``inline.txt``, and the
lexical document node is keyed on that name, so each write overwrote the last. These pin the two
halves of the fix — a derived human name, and a document key that is the artifact's own identity.

The content samples are the real first lines of that run's artifacts.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_knowledge_graph_service.domain.artifact_naming import (
    MAX_NAME_LEN,
    ProducerRef,
    derive_name,
    document_key,
    title_from_content,
)

# The real opening lines from run dc167d8e (base64-decoded from ingestion_jobs.source_content).
EURAIL_HEADINGS = [
    (
        "### Consequences of Inaction for Interrail B.V. Without AI Adoption\n\n1. **Falling**",
        "Consequences of Inaction for Interrail B.V. Without AI Adoption",
    ),
    (
        "### Driving Signals for Consequences of Inaction\n- x",
        "Driving Signals for Consequences of Inaction",
    ),
    (
        "### Opportunities for AI Adoption at Interrail B.V.\n\n1. **Reservation**",
        "Opportunities for AI Adoption at Interrail B.V.",
    ),
    (
        "### Urgent Decisions for Interrail's Board Regarding AI Strategy\n\n1. **Investment**",
        "Urgent Decisions for Interrail's Board Regarding AI Strategy",
    ),
]

# The one artifact of the seven with no title: a raw driving_signals payload.
EURAIL_JSON = '[{"signal":"Interrail B.V. has a complex reservation process.","value":"x"}]'


class TestTitleFromContent:
    @pytest.mark.parametrize(("content", "expected"), EURAIL_HEADINGS)
    def test_a_markdown_heading_is_the_title(self, content: str, expected: str) -> None:
        assert title_from_content(content) == expected

    def test_a_json_payload_has_no_title(self) -> None:
        assert title_from_content(EURAIL_JSON) is None

    def test_leading_blank_lines_are_skipped(self) -> None:
        assert title_from_content("\n\n\n## Real Title\nbody") == "Real Title"

    def test_a_closed_atx_heading_drops_its_trailing_hashes(self) -> None:
        assert title_from_content("## Quarterly Review ##") == "Quarterly Review"

    def test_a_body_paragraph_is_not_a_title(self) -> None:
        assert (
            title_from_content("This document explains the reasoning behind the decision.") is None
        )

    def test_a_plain_first_line_may_serve_as_a_title(self) -> None:
        assert title_from_content("Board Briefing 2026\n\nbody text") == "Board Briefing 2026"

    def test_empty_content_has_no_title(self) -> None:
        assert title_from_content("") is None

    def test_path_and_control_characters_are_stripped(self) -> None:
        assert title_from_content("# a/b:c*d?e") == "abcde"

    def test_an_over_long_title_is_truncated(self) -> None:
        derived = title_from_content("# " + "x" * (MAX_NAME_LEN + 50))
        assert derived is not None
        assert len(derived) <= MAX_NAME_LEN


class TestDeriveName:
    def test_an_explicit_title_wins(self) -> None:
        assert derive_name("### From Content", title="From Caller") == "From Caller"

    def test_content_is_used_when_no_title_is_supplied(self) -> None:
        assert derive_name("### From Content\nbody") == "From Content"

    def test_the_producer_names_an_untitled_artifact(self) -> None:
        producer = ProducerRef(kind="team-member", member_role="Adoption-writer", ordinal=2)
        assert derive_name(EURAIL_JSON, producer=producer) == "Adoption-writer output 2"

    def test_the_producer_without_an_ordinal_is_the_role_alone(self) -> None:
        producer = ProducerRef(kind="team-member", member_role="Editor")
        assert derive_name(EURAIL_JSON, producer=producer) == "Editor"

    def test_nothing_at_all_still_yields_a_name(self) -> None:
        assert derive_name(EURAIL_JSON) == "artifact"

    def test_no_artifact_is_ever_named_inline_txt(self) -> None:
        """The regression this issue exists for."""
        producer = ProducerRef(kind="team-member", member_role="Partner-writer", ordinal=0)
        for content, _ in EURAIL_HEADINGS:
            assert derive_name(content, producer=producer) != "inline.txt"
        assert derive_name(EURAIL_JSON, producer=producer) != "inline.txt"

    def test_the_seven_eurail_artifacts_are_distinguishable_by_name(self) -> None:
        """A person reading /v1/artifacts can tell them apart without opening them."""
        names = [derive_name(c) for c, _ in EURAIL_HEADINGS]
        names.append(
            derive_name(
                EURAIL_JSON,
                producer=ProducerRef(kind="team-member", member_role="Adoption-writer", ordinal=3),
            )
        )
        assert len(set(names)) == len(names)

    def test_a_blank_explicit_title_falls_through_to_content(self) -> None:
        assert derive_name("### Real Title", title="   ") == "Real Title"

    def test_derivation_is_deterministic(self) -> None:
        producer = ProducerRef(kind="team-member", member_role="Editor", ordinal=1)
        assert derive_name(EURAIL_JSON, producer=producer) == derive_name(
            EURAIL_JSON, producer=producer
        )


class TestDocumentKey:
    def test_an_agent_artifact_keys_on_its_own_id(self) -> None:
        artifact_id = uuid.uuid4()
        producer = ProducerRef(kind="team-member", member_role="Adoption-writer")
        assert document_key(artifact_id=artifact_id, producer=producer) == str(artifact_id)

    def test_two_members_writing_the_same_title_get_two_keys(self) -> None:
        """The exact collision of run dc167d8e: same name, different artifacts, one node."""
        first = ProducerRef(kind="team-member", member_role="Adoption-writer")
        second = ProducerRef(kind="team-member", member_role="Partner-writer")
        key_a = document_key(artifact_id=uuid.uuid4(), producer=first)
        key_b = document_key(artifact_id=uuid.uuid4(), producer=second)
        assert key_a != key_b

    def test_a_standalone_agent_also_keys_on_its_id(self) -> None:
        artifact_id = uuid.uuid4()
        producer = ProducerRef(kind="standalone-agent")
        assert document_key(artifact_id=artifact_id, producer=producer) == str(artifact_id)

    def test_a_user_upload_refuses_id_keying(self) -> None:
        """#522: re-ingesting the same path REPLACES, so an upload keys on its filename."""
        with pytest.raises(ValueError, match="filename"):
            document_key(artifact_id=uuid.uuid4(), producer=ProducerRef(kind="user-upload"))

    def test_no_producer_refuses_id_keying(self) -> None:
        with pytest.raises(ValueError):
            document_key(artifact_id=uuid.uuid4(), producer=None)
