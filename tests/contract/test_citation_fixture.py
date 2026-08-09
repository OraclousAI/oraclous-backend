"""Reference self-test for the citation contract fixture (#742, Contract #735, §CITE rev3).

This proves the shared artifacts under ``packages/citation/contract/`` are internally consistent,
that the JSON Schema enforces the §CITE constraints, and that every sample's ``citation_id`` is the
real digest of its own three identity fields. The backend models and the frontend console consume
the *same* bytes; this is the fixture's own enforcement that it is correct before either side
depends on it.

Deliberately **seam-free**: it touches no ``oraclous_citation`` module, so it is GREEN the moment
the fixture lands. ``oraclous-frontend#194`` is blocked on these bytes and unblocks when this
``[tests]`` PR merges, not when the ``[impl]`` does — a fixture nobody has verified is not an
unblock.

Marked ``unit`` (fast, isolated) and ``contract`` (a cross-repo shape).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

pytestmark = [pytest.mark.unit, pytest.mark.contract]

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "packages" / "citation" / "contract"
SCHEMA_PATH = CONTRACT_DIR / "citation.schema.json"
SAMPLES_DIR = CONTRACT_DIR / "samples"
CHECKSUMS_PATH = CONTRACT_DIR / "CHECKSUMS.sha256"

#: One sample per rendering case the console must handle (§CITE rev3, four samples).
EXPECTED_SAMPLES = {"github", "web", "web-no-author", "upload"}

#: §CITE lists these seven and no others: six source-native version kinds plus our own fallback.
EXPECTED_REVISION_KINDS = {
    "commit",
    "blob_sha",
    "version_id",
    "etag",
    "edited_at",
    "block",
    "content_hash",
}

#: Every key a complete Citation carries. "Never partly filled" is encoded as: all ten are
#: REQUIRED, and the ones that may legitimately be unknown are nullable rather than absent.
CITATION_KEYS = {
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
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _samples() -> dict[str, dict[str, Any]]:
    return {p.stem: _load(p) for p in sorted(SAMPLES_DIR.glob("*.json"))}


def _expected_citation_id(source_system: str, source_id: str, revision: str) -> str:
    """The §CITE identity, recomputed here from first principles rather than imported.

    The separator is a NUL BYTE, not a literal string — recomputing it independently is what makes
    the samples a real cross-check on the algorithm instead of four numbers somebody typed.
    """
    payload = b"\x00".join(part.encode("utf-8") for part in (source_system, source_id, revision))
    return "cit_" + hashlib.sha256(payload).hexdigest()[:32]


def _walk_object_schemas(node: Any) -> list[dict[str, Any]]:
    """Every subschema that declares ``"type": "object"``, anywhere in the document."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(_walk_object_schemas(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_object_schemas(item))
    return found


def _all_property_names(node: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names |= set(properties)
        for value in node.values():
            names |= _all_property_names(value)
    elif isinstance(node, list):
        for item in node:
            names |= _all_property_names(item)
    return names


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return _load(SCHEMA_PATH)


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Validates a **Citation** — the schema root."""
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def source_ref_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Validates a **SourceRef** — what a connector emits inbound."""
    return Draft202012Validator({**schema, "$ref": "#/$defs/SourceRef"})


def _citation(**overrides: Any) -> dict[str, Any]:
    """A minimal valid Citation, for negative-case mutation."""
    base: dict[str, Any] = {
        "citation_id": _expected_citation_id("github", "o/r:a.md", "abc123"),
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
    base.update(overrides)
    return base


# --- schema sanity ---------------------------------------------------------


def test_schema_is_itself_valid(schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)  # raises if the schema is malformed


def test_schema_defines_citation_source_ref_and_author(schema: dict[str, Any]) -> None:
    assert set(schema["$defs"]) == {"Citation", "SourceRef", "Author"}


def test_additional_properties_false_at_every_level(schema: dict[str, Any]) -> None:
    """No caller-smuggled field can ever exist — including a reintroduced locator."""
    object_schemas = _walk_object_schemas(schema)
    assert object_schemas, "schema declares no object subschemas — the walk is broken"
    for sub in object_schemas:
        assert sub.get("additionalProperties") is False, (
            f"object subschema {sub.get('title') or sorted(sub.get('properties', {}))} "
            "does not set additionalProperties: false"
        )


def test_citation_requires_all_ten_keys(schema: dict[str, Any]) -> None:
    """A citation is either fully absent or complete — there is no half-citation."""
    citation = schema["$defs"]["Citation"]
    assert set(citation["required"]) == CITATION_KEYS
    assert set(citation["properties"]) == CITATION_KEYS


def test_revision_kind_is_the_closed_set_of_seven(schema: dict[str, Any]) -> None:
    enum = schema["$defs"]["Citation"]["properties"]["revision_kind"]["enum"]
    assert set(enum) == EXPECTED_REVISION_KINDS
    assert len(enum) == len(EXPECTED_REVISION_KINDS)


def test_source_system_is_not_a_closed_enum(schema: dict[str, Any]) -> None:
    """UC-E1 acceptance 6: a sixth, unplanned source type must be connectable without
    re-modelling existing records. A frozen enum would break that."""
    source_system = schema["$defs"]["Citation"]["properties"]["source_system"]
    assert "enum" not in source_system
    assert "const" not in source_system


# --- the locator is not in v1 ----------------------------------------------


def test_no_locator_property_anywhere_in_the_schema(schema: dict[str, Any]) -> None:
    """rev3: a citation resolves to a document version and nothing finer. The field does not
    exist in the model, the schema, or the samples — not even as an optional one."""
    forbidden = {"locator", "chunk_index", "chunk", "line_range", "page", "anchor", "offset"}
    assert not (_all_property_names(schema) & forbidden)


def test_no_locator_key_in_any_sample() -> None:
    forbidden = {"locator", "chunk_index", "chunk", "line_range", "page", "anchor", "offset"}
    for name, sample in _samples().items():
        assert not (set(sample) & forbidden), f"{name} sample carries a sub-document key"


# --- samples ---------------------------------------------------------------


def test_the_four_rendering_cases_are_present() -> None:
    assert set(_samples()) == EXPECTED_SAMPLES


@pytest.mark.parametrize("name", sorted(EXPECTED_SAMPLES))
def test_sample_validates_against_the_schema(name: str, validator: Draft202012Validator) -> None:
    errors = [e.message for e in validator.iter_errors(_samples()[name])]
    assert not errors, errors


@pytest.mark.parametrize("name", sorted(EXPECTED_SAMPLES))
def test_sample_citation_id_is_the_digest_of_its_own_three_fields(name: str) -> None:
    """The identity is keyed on exactly (source_system, source_id, revision) — nothing else in
    the sample may influence it. Recomputed independently, NUL separators included."""
    sample = _samples()[name]
    assert sample["citation_id"] == _expected_citation_id(
        sample["source_system"], sample["source_id"], sample["revision"]
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_SAMPLES))
def test_sample_revision_is_never_null(name: str) -> None:
    """Global rule, not an ingest-only one: a source with no version of its own still carries a
    content-hash revision, so the identity hash and the supersession rule stay total."""
    sample = _samples()[name]
    assert isinstance(sample["revision"], str) and sample["revision"]


def test_upload_sample_carries_a_job_id_a_content_hash_and_no_url() -> None:
    """The one source the platform itself cannot resolve today. #745 gives it a url."""
    upload = _samples()["upload"]
    assert upload["source_system"] == "upload"
    assert upload["revision_kind"] == "content_hash"
    assert upload["url"] is None


def test_web_sample_has_a_content_hash_revision_not_a_null() -> None:
    """§CITE-QUAL's `document` grade gives a link but no source-native version. rev3 resolved that
    in favour of the stricter rule: the platform hashes the content it holds."""
    web = _samples()["web"]
    assert web["revision_kind"] == "content_hash"
    assert web["url"] is not None
    assert web["revision"] != web["url"]


def test_author_less_case_is_a_null_author_not_an_empty_object() -> None:
    """Many sources expose no author. Null is the shape; a stub author object is a fabricated
    one."""
    assert _samples()["web-no-author"]["author"] is None
    assert _samples()["github"]["author"]["display_name"]


# --- negative schema cases -------------------------------------------------


def test_rejects_a_locator_field(validator: Draft202012Validator) -> None:
    assert not validator.is_valid(_citation(locator="chunk7"))


def test_rejects_null_revision(validator: Draft202012Validator) -> None:
    assert not validator.is_valid(_citation(revision=None))


def test_rejects_a_missing_revision_kind(validator: Draft202012Validator) -> None:
    partial = _citation()
    del partial["revision_kind"]
    assert not validator.is_valid(partial)


def test_rejects_a_revision_kind_outside_the_seven(validator: Draft202012Validator) -> None:
    assert not validator.is_valid(_citation(revision_kind="vibes"))


@pytest.mark.parametrize("bad", ["9f2a4c81", "cit_", "cit_" + "Z" * 32, "cit_" + "a" * 31, "abc"])
def test_rejects_a_malformed_citation_id(bad: str, validator: Draft202012Validator) -> None:
    assert not validator.is_valid(_citation(citation_id=bad))


def test_rejects_a_relative_url(validator: Draft202012Validator) -> None:
    assert not validator.is_valid(_citation(url="/uploads/report.pdf"))


def test_rejects_a_free_text_retrieved_at(validator: Draft202012Validator) -> None:
    assert not validator.is_valid(_citation(retrieved_at="last Tuesday"))


def test_rejects_an_author_with_an_extra_key(validator: Draft202012Validator) -> None:
    author = {"display_name": "Dana Okoro", "source_handle": "danaokoro", "password": "hunter2"}
    assert not validator.is_valid(_citation(author=author))


def test_rejects_an_author_with_no_display_name(validator: Draft202012Validator) -> None:
    assert not validator.is_valid(_citation(author={"source_handle": "danaokoro"}))


@pytest.mark.parametrize("key", sorted(CITATION_KEYS))
def test_a_half_citation_missing_any_key_is_rejected(
    key: str, validator: Draft202012Validator
) -> None:
    """Never partly filled: dropping ANY of the ten keys is invalid, not "the rest is fine"."""
    partial = _citation()
    del partial[key]
    assert not validator.is_valid(partial), f"a citation missing {key!r} validated"


# --- SourceRef: what may travel inbound ------------------------------------


def test_source_ref_accepts_a_connector_emission(
    source_ref_validator: Draft202012Validator,
) -> None:
    ref = {
        "source_system": "github",
        "source_id": "OraclousAI/oraclous-backend:README.md",
        "revision": "9f2a4c81b3d7e5061c8a4f2b6d90e37c15ab8e42",
        "revision_kind": "blob_sha",
        "url": "https://github.com/OraclousAI/oraclous-backend/blob/9f2a4c81/README.md",
        "title": "README.md",
        "author": None,
        "permission_ref": None,
    }
    assert source_ref_validator.is_valid(ref)


def test_source_ref_accepts_a_bare_identity_with_no_revision(
    source_ref_validator: Draft202012Validator,
) -> None:
    """`revision`/`revision_kind` are optional INBOUND — the platform falls back to a content
    hash. Everything else about the shape is as §CITE defines it."""
    assert source_ref_validator.is_valid(
        {"source_system": "web-research", "source_id": "https://x"}
    )


def test_source_ref_rejects_a_revision_without_its_kind(
    source_ref_validator: Draft202012Validator,
) -> None:
    """A version with no statement of what kind it is cannot be told apart from our own fallback,
    which is the one thing `revision_kind` exists to do."""
    bad = {"source_system": "github", "source_id": "o/r:a.md", "revision": "abc123"}
    assert not source_ref_validator.is_valid(bad)


@pytest.mark.parametrize("forged", ["citation_id", "retrieved_at"])
def test_source_ref_cannot_carry_a_platform_computed_field(
    forged: str, source_ref_validator: Draft202012Validator
) -> None:
    """The platform mints these. additionalProperties:false makes a caller-supplied one
    unrepresentable at the inbound boundary — the schema half of the anti-forgery rule."""
    ref = {"source_system": "github", "source_id": "o/r:a.md", forged: "anything"}
    assert not source_ref_validator.is_valid(ref)


# --- checksum integrity ----------------------------------------------------


def test_checksum_manifest_matches_artifacts() -> None:
    """The drift guard. The frontend mirrors this against its copied fixture, so a divergence on
    either side breaks CI (Cross-cutting agreement protocol §2.6)."""
    recorded: dict[str, str] = {}
    for line in CHECKSUMS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, _, rel = line.partition("  ")
            recorded[rel.strip()] = digest.strip()

    actual = {
        path.relative_to(CONTRACT_DIR).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [SCHEMA_PATH, *sorted(SAMPLES_DIR.glob("*.json"))]
    }
    assert recorded == actual
