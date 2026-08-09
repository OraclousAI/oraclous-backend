"""The models agree with the shared fixture bytes, and with the graph store (#742, §CITE rev3).

Two things are pinned here.

**Conformance.** ``oraclous_citation``'s models and the JSON Schema under
``packages/citation/contract/`` are one shape, not two that resemble each other. This is asserted
**once, in the shared package**, rather than per service: both ``knowledge-graph-service`` (write)
and ``knowledge-retriever-service`` (read) consume the same models, so a per-service copy of this
test would assert the same fact twice and let the two drift apart in between.

**The graph-store encoding.** A Neo4j property is a primitive — a nested ``citation`` map cannot be
stored on a node. §CITE leaves the encoding to the implementing brief ("mechanism, not shape"), but
it cannot be left to each service: the writer flattens and the retriever lifts, they live in
different services, and they must agree exactly. So the pair lives in the shared package, and these
tests pin the round trip WITHOUT pinning the key names — those stay the implementer's choice.

Every import of ``oraclous_citation`` is **function-local** (TST001); the tests hard-fail RED with
``ModuleNotFoundError`` until the paired ``[impl]`` lands, and never skip.

RED until backend-implementer creates ``oraclous_citation``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.unit

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contract"
SAMPLE_NAMES = ["github", "web", "web-no-author", "upload"]


def _schema() -> dict[str, Any]:
    return json.loads((CONTRACT_DIR / "citation.schema.json").read_text(encoding="utf-8"))


def _sample(name: str) -> dict[str, Any]:
    return json.loads((CONTRACT_DIR / "samples" / f"{name}.json").read_text(encoding="utf-8"))


class TestTheModelAndTheFixtureAreOneShape:
    """The frontend validates against the JSON Schema; the backend validates against the models.
    If those two disagree, the Contract is enforced on neither side."""

    @pytest.mark.parametrize("name", SAMPLE_NAMES)
    def test_every_sample_loads_into_the_model(self, name: str) -> None:
        from oraclous_citation import Citation  # RED

        citation = Citation.model_validate(_sample(name))
        assert citation.citation_id == _sample(name)["citation_id"]

    @pytest.mark.parametrize("name", SAMPLE_NAMES)
    def test_every_sample_round_trips_through_the_model_unchanged(self, name: str) -> None:
        """Load and dump must be lossless. A field the model silently drops is a field the
        console renders and the backend cannot produce."""
        from oraclous_citation import Citation  # RED

        sample = _sample(name)
        assert Citation.model_validate(sample).model_dump(mode="json") == sample

    def test_a_minted_citation_validates_against_the_shared_schema(self) -> None:
        from oraclous_citation import SourceRef, mint_citation  # RED

        citation = mint_citation(
            SourceRef(
                source_system="github",
                source_id="OraclousAI/oraclous-backend:README.md",
                revision="9f2a4c81b3d7e5061c8a4f2b6d90e37c15ab8e42",
                revision_kind="blob_sha",
                url="https://github.com/OraclousAI/oraclous-backend/blob/9f2a4c81/README.md",
                title="README.md",
            )
        )
        errors = [
            e.message
            for e in Draft202012Validator(_schema()).iter_errors(citation.model_dump(mode="json"))
        ]
        assert not errors, errors

    def test_the_model_rejects_a_locator(self) -> None:
        """rev3 deleted it. A model that quietly accepts one is how it comes back."""
        from oraclous_citation import Citation  # RED
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Citation(**{**_sample("github"), "locator": "chunk7"})


class TestTheGraphStoreEncoding:
    """The writer flattens a citation onto a node; the retriever lifts it back. Neither may
    invent its own encoding, so the pair is shared."""

    def test_the_flattened_form_is_storable_as_neo4j_properties(self) -> None:
        """Every value must be a primitive — Neo4j cannot store a nested map on a node."""
        from oraclous_citation import Citation, citation_properties  # RED

        for value in citation_properties(Citation.model_validate(_sample("github"))).values():
            assert isinstance(value, (str, int, float, bool, type(None))), value

    @pytest.mark.parametrize("name", SAMPLE_NAMES)
    def test_flatten_then_lift_returns_the_same_citation(self, name: str) -> None:
        from oraclous_citation import Citation, citation_from_properties, citation_properties  # RED

        citation = Citation.model_validate(_sample(name))
        assert citation_from_properties(citation_properties(citation)) == citation

    def test_lifting_ignores_the_rest_of_the_node(self) -> None:
        """A stored Chunk also carries text, graph_id, organisation_id and the bitemporal stamps."""
        from oraclous_citation import Citation, citation_from_properties, citation_properties  # RED

        citation = Citation.model_validate(_sample("web"))
        stored = {
            **citation_properties(citation),
            "text": "the chunk body",
            "graph_id": "graph-A",
            "organisation_id": "org-1",
            "ingestion_source": "partner-agreements",
        }
        assert citation_from_properties(stored) == citation

    def test_a_pre_contract_record_lifts_to_none(self) -> None:
        """Criterion 9 / §CITE: ``citation: null`` means "ingested before this Contract". There
        is no backfill — synthesising source identity is the forged citation the gate rejects."""
        from oraclous_citation import citation_from_properties  # RED

        assert citation_from_properties({"text": "an older chunk", "graph_id": "graph-A"}) is None

    def test_a_partial_stored_citation_lifts_to_none_not_to_a_half_citation(self) -> None:
        """Never partly filled. A node carrying only some of the citation properties — a partial
        write, a hand-edited node — resolves to nothing, never to a citation with holes in it.

        Only properties the sample actually populates are dropped: an encoding is free to omit a
        null-valued property rather than store an explicit null, so removing one of those is not
        the same as truncating the record.
        """
        from oraclous_citation import Citation, citation_from_properties, citation_properties  # RED

        full = citation_properties(Citation.model_validate(_sample("github")))
        populated = [key for key, value in full.items() if value is not None]
        assert populated, "the flattened github sample carries no populated property"
        for dropped in sorted(populated):
            partial = {k: v for k, v in full.items() if k != dropped}
            assert citation_from_properties(partial) is None, (
                f"a citation missing the {dropped!r} property was lifted anyway"
            )
