"""The deterministic ``citation_id`` — the whole security mechanism (#742, §CITE rev3).

```
citation_id = "cit_" + sha256(source_system ‖ 0x00 ‖ source_id ‖ 0x00 ‖ revision)[:32]
```

Three fields, two **NUL bytes**, 32 hex characters. Determinism buys three things at once:
re-reading the same revision yields the same id (an idempotent refresh); a new revision yields a
different id, which makes supersession computable without a supersession table; and an id a model
invents is not in the set the platform served, so it fails the answer-time gate.

Every import of ``oraclous_citation`` is **function-local** (TST001) so this module collects
cleanly during the TDD window. The tests fail at runtime with ``ModuleNotFoundError`` until the
paired ``[impl]`` lands — RED by design, never a skip (`.claude/rules/tests-seam-imports.md`).

RED until backend-implementer creates ``oraclous_citation``.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


def _digest(source_system: str, source_id: str, revision: str) -> str:
    """The expected id, recomputed here from first principles — never imported from the SUT."""
    payload = b"\x00".join(part.encode("utf-8") for part in (source_system, source_id, revision))
    return "cit_" + hashlib.sha256(payload).hexdigest()[:32]


class TestTheIdentityIsTheDigestOfThreeFields:
    """``citation_id`` is keyed on exactly (source_system, source_id, revision)."""

    def test_id_matches_the_independently_computed_digest(self) -> None:
        from oraclous_citation import compute_citation_id  # RED

        actual = compute_citation_id("github", "OraclousAI/oraclous-backend:README.md", "9f2a4c81")
        assert actual == _digest("github", "OraclousAI/oraclous-backend:README.md", "9f2a4c81")

    def test_id_carries_the_cit_prefix_and_32_hex_characters(self) -> None:
        from oraclous_citation import compute_citation_id  # RED

        cid = compute_citation_id("upload", "b7f1c0de-4a2e-4d3f-9c61-0f8e5a2b6d14", "deadbeef")
        assert cid.startswith("cit_")
        digest = cid.removeprefix("cit_")
        assert len(digest) == 32
        assert all(c in "0123456789abcdef" for c in digest)

    def test_the_separator_is_a_nul_byte_not_a_literal(self) -> None:
        """§CITE writes the separator as ``‖ 0x00 ‖``. A literal separator — a pipe, the ‖ glyph,
        an empty string — produces a different digest, and a fixture minted under one convention
        would stop resolving against the other."""
        from oraclous_citation import compute_citation_id  # RED

        parts = ("github", "o/r:a.md", "abc123")
        actual = compute_citation_id(*parts)
        for wrong_separator in ("", "|", "‖", ":", "\n"):
            wrong = (
                "cit_"
                + hashlib.sha256(wrong_separator.join(parts).encode("utf-8")).hexdigest()[:32]
            )
            assert actual != wrong, f"the id matches a {wrong_separator!r}-joined digest"

    def test_the_id_is_stable_across_processes(self) -> None:
        """No per-process salt, no ``hash()``, no clock. A citation stored in a published answer
        must still resolve after a redeploy — that is what makes supersession computable."""
        from oraclous_citation import compute_citation_id  # RED

        in_process = compute_citation_id("github", "o/r:a.md", "abc123")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from oraclous_citation import compute_citation_id;"
                "print(compute_citation_id('github', 'o/r:a.md', 'abc123'))",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == in_process


class TestSupersession:
    """A new revision yields a different id. Without this, "this replaced that" is not
    computable, and the Contract carries no supersession table to fall back on."""

    def test_a_new_revision_yields_a_different_id(self) -> None:
        from oraclous_citation import compute_citation_id  # RED

        before = compute_citation_id("github", "o/r:a.md", "rev-1")
        after = compute_citation_id("github", "o/r:a.md", "rev-2")
        assert before != after

    def test_the_same_revision_yields_the_same_id(self) -> None:
        """An idempotent refresh: re-reading an unchanged document does not mint a new record."""
        from oraclous_citation import compute_citation_id  # RED

        assert compute_citation_id("github", "o/r:a.md", "rev-1") == compute_citation_id(
            "github", "o/r:a.md", "rev-1"
        )

    def test_a_different_source_system_yields_a_different_id(self) -> None:
        from oraclous_citation import compute_citation_id  # RED

        assert compute_citation_id("github", "x", "r") != compute_citation_id("notion", "x", "r")

    def test_a_different_source_id_yields_a_different_id(self) -> None:
        from oraclous_citation import compute_citation_id  # RED

        assert compute_citation_id("github", "o/r:a.md", "r") != compute_citation_id(
            "github", "o/r:b.md", "r"
        )


class TestNothingElseEntersTheIdentity:
    """Only the three fields are hashed. Anything else in the identity would churn ids for a
    document that never changed."""

    def test_the_url_is_not_part_of_the_identity(self) -> None:
        """A repository rename or a moved permalink must not invalidate a stored citation."""
        from oraclous_citation import Citation, compute_citation_id  # RED

        expected = compute_citation_id("github", "o/r:a.md", "abc123")
        for url in (
            "https://github.com/o/r/blob/abc123/a.md",
            "https://github.example.com/o/r/blob/abc123/a.md",
        ):
            citation = Citation(
                citation_id=expected,
                source_system="github",
                source_id="o/r:a.md",
                revision="abc123",
                revision_kind="blob_sha",
                url=url,
                title="a.md",
                author=None,
                retrieved_at="2026-08-08T09:14:22Z",
                permission_ref=None,
            )
            assert citation.citation_id == expected

    def test_no_sub_document_argument_is_accepted(self) -> None:
        """rev3 deleted the locator. ``compute_citation_id`` takes three arguments and no fourth —
        reintroducing sub-document precision into the identity is what this guards against."""
        from oraclous_citation import compute_citation_id  # RED

        with pytest.raises(TypeError):
            compute_citation_id("github", "o/r:a.md", "abc123", "chunk7")  # type: ignore[call-arg]
