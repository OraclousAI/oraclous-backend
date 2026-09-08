"""#643 + #949 (C1) — the shared `oraclous_embedding` package both services build their embedder
from, so the write side (knowledge-graph-service) and the read side (knowledge-retriever-service)
can never spell the same embedder two different ways again.

Today each service carries its OWN copy: `knowledge-graph-service/services/embedder.py` (the full
seam: `Embedder` Protocol, `HashingEmbedder`, `OpenAIEmbedder`, `make_embedder`) and
`knowledge-retriever-service/services/embedder.py` (a lone, hand-duplicated `HashingEmbedder` whose
own docstring says "BYTE-IDENTICAL... If the two ever diverge, semantic search silently degrades").
#643 exists because that promise is unenforced. This pins the ONE shared implementation both
services must import (never re-implement), plus the identity function (`embedder_id`) that C3 (write
+ read) both key off, so a spelling drift becomes a merge conflict, not a silent divergence.

`oraclous_embedding` does not exist yet. Every seam import below is FUNCTION-LOCAL
(`.claude/rules/tests-seam-imports.md`) — this file collects cleanly and each test hard-fails RED
with `ModuleNotFoundError` at runtime until the `[impl]` lands the package.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import pytest

pytestmark = pytest.mark.unit


@dataclass
class _Settings:
    """The minimal duck-typed settings surface `make_embedder`/`embedder_identity` need — the
    shared package must not import either service's concrete `Settings` (it would be an upward
    dependency neither service can have on the other, and packages/ never imports a service)."""

    embedder: Literal["hashing", "openai"] = "hashing"
    embedding_dim: int = 512


class _Credential:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key


# --- HashingEmbedder: byte-identical to a fixed vector -----------------------------------------


def test_hashing_embedder_is_byte_identical_to_a_fixed_vector() -> None:
    """Pins the exact output for a fixed input at the production dimension (512), so a future
    refactor of the shared package cannot silently change the vector space out from under every
    chunk already written in it. Computed independently from the blake2b feature-hashing algorithm
    both existing copies implement today (digest_size=8, dim=512, L2-normalised)."""
    from oraclous_embedding import HashingEmbedder  # noqa: PLC0415

    embedder = HashingEmbedder(dim=512)
    (vector,) = embedder.embed(["hello world"])

    assert len(vector) == 512
    nonzero = {i: round(v, 6) for i, v in enumerate(vector) if abs(v) > 1e-9}
    assert nonzero == {125: 0.707107, 271: -0.707107}
    assert abs(math.sqrt(sum(v * v for v in vector)) - 1.0) < 1e-9


def test_hashing_embedder_is_deterministic_across_instances() -> None:
    from oraclous_embedding import HashingEmbedder  # noqa: PLC0415

    a = HashingEmbedder(dim=64).embed(["ada lovelace"])
    b = HashingEmbedder(dim=64).embed(["ada lovelace"])
    assert a == b


def test_hashing_embedder_empty_text_is_the_zero_vector() -> None:
    from oraclous_embedding import HashingEmbedder  # noqa: PLC0415

    (vector,) = HashingEmbedder(dim=32).embed([""])
    assert vector == [0.0] * 32


# --- shape: embed() takes list[str], returns list[list[float]] ---------------------------------


def test_embed_takes_a_list_and_returns_a_list_of_vectors() -> None:
    """The write side is `embed(texts: list[str]) -> list[list[float]]`; the read side used to be
    a scalar `embed(text: str) -> list[float]`. The shared seam settles on the LIST shape — a
    caller with one query embeds `embed([query])[0]` (pinned on the retriever's own call site in
    the KRS resolution tests, not here)."""
    from oraclous_embedding import HashingEmbedder  # noqa: PLC0415

    out = HashingEmbedder(dim=16).embed(["one", "two", "three"])
    assert isinstance(out, list)
    assert len(out) == 3
    assert all(isinstance(v, list) and len(v) == 16 for v in out)


def test_embed_of_empty_list_is_empty_list() -> None:
    from oraclous_embedding import HashingEmbedder  # noqa: PLC0415

    assert HashingEmbedder(dim=8).embed([]) == []


# --- Embedder Protocol --------------------------------------------------------------------------


def test_hashing_and_openai_embedders_satisfy_the_embedder_protocol() -> None:
    from oraclous_embedding import Embedder, HashingEmbedder, OpenAIEmbedder  # noqa: PLC0415

    assert isinstance(HashingEmbedder(dim=8), Embedder)
    assert isinstance(OpenAIEmbedder(api_key="sk-x", dim=8), Embedder)


# --- embedder_identity(settings) -> str: ONE function, both sides read it ----------------------


def test_embedder_identity_hashing() -> None:
    from oraclous_embedding import embedder_identity  # noqa: PLC0415

    assert embedder_identity(_Settings(embedder="hashing", embedding_dim=512)) == "hashing:512"


def test_embedder_identity_hashing_reflects_a_non_default_dimension() -> None:
    from oraclous_embedding import embedder_identity  # noqa: PLC0415

    assert embedder_identity(_Settings(embedder="hashing", embedding_dim=64)) == "hashing:64"


def test_embedder_identity_openai() -> None:
    from oraclous_embedding import embedder_identity  # noqa: PLC0415

    identity = embedder_identity(_Settings(embedder="openai", embedding_dim=512))
    assert identity == "openai:text-embedding-3-small:512"


def test_embedder_identity_is_the_only_place_the_string_is_spelled() -> None:
    """Both C3 write (stamping `:Chunk.embedder_id`) and C3 read (the semantic Cypher's
    `embedder_id` filter) must call this SAME function — that is the entire fix for #643's "the two
    embedders can spell their identity differently" failure mode. This asserts the function is
    pure and side-effect-free (repeatable), which is what makes it safe to call from both a Celery
    worker (write) and a FastAPI request (read)."""
    from oraclous_embedding import embedder_identity  # noqa: PLC0415

    settings = _Settings(embedder="openai", embedding_dim=512)
    assert embedder_identity(settings) == embedder_identity(settings)


# --- make_embedder(settings, *, credential=None) --------------------------------------------


def test_make_embedder_hashing_needs_no_credential() -> None:
    from oraclous_embedding import HashingEmbedder, make_embedder  # noqa: PLC0415

    embedder = make_embedder(_Settings(embedder="hashing", embedding_dim=512))
    assert isinstance(embedder, HashingEmbedder)
    assert embedder.dim == 512


def test_make_embedder_openai_with_a_credential() -> None:
    from oraclous_embedding import OpenAIEmbedder, make_embedder  # noqa: PLC0415

    embedder = make_embedder(
        _Settings(embedder="openai", embedding_dim=512), credential=_Credential("sk-org-key")
    )
    assert isinstance(embedder, OpenAIEmbedder)


def test_make_embedder_openai_without_a_credential_fails_closed() -> None:
    """Fail-closed (§3.5): the openai mode with no credential must refuse, never silently
    substitute the hashing embedder under an `openai` label — the exact #949 Q4 failure mode."""
    from oraclous_embedding import make_embedder  # noqa: PLC0415

    with pytest.raises(Exception):  # noqa: B017, PLR1714 — the seam's own exception type
        make_embedder(_Settings(embedder="openai", embedding_dim=512), credential=None)


# --- neither service keeps its own copy ----------------------------------------------------


def test_kgs_embedder_module_reexports_the_shared_hashing_embedder() -> None:
    """knowledge-graph-service must import the shared class, not define a structurally-identical
    duplicate — an `is` check is the only pin strong enough to catch a copy-paste that keeps the
    algorithm byte-identical today but can drift on the next edit to either copy."""
    from oraclous_embedding import HashingEmbedder as Shared  # noqa: PLC0415
    from oraclous_knowledge_graph_service.services.embedder import (  # noqa: PLC0415
        HashingEmbedder as KGSHashingEmbedder,
    )

    assert KGSHashingEmbedder is Shared


def test_kgs_embedder_module_reexports_the_shared_openai_embedder() -> None:
    from oraclous_embedding import OpenAIEmbedder as Shared  # noqa: PLC0415
    from oraclous_knowledge_graph_service.services.embedder import (  # noqa: PLC0415
        OpenAIEmbedder as KGSOpenAIEmbedder,
    )

    assert KGSOpenAIEmbedder is Shared


def test_krs_embedder_module_reexports_the_shared_hashing_embedder() -> None:
    """knowledge-retriever-service's own docstring calls its copy "BYTE-IDENTICAL... keep them in
    lockstep" — the fix for #643 is that there is only one copy to keep in lockstep with."""
    from oraclous_embedding import HashingEmbedder as Shared  # noqa: PLC0415
    from oraclous_knowledge_retriever_service.services.embedder import (  # noqa: PLC0415
        HashingEmbedder as KRSHashingEmbedder,
    )

    assert KRSHashingEmbedder is Shared


def test_krs_embedder_module_reexports_the_shared_openai_embedder() -> None:
    """KRS has never had an OpenAIEmbedder of its own (#643's whole premise) — this is new."""
    from oraclous_embedding import OpenAIEmbedder as Shared  # noqa: PLC0415
    from oraclous_knowledge_retriever_service.services.embedder import (  # noqa: PLC0415
        OpenAIEmbedder as KRSOpenAIEmbedder,
    )

    assert KRSOpenAIEmbedder is Shared
