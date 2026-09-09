"""Test fixtures for oraclous-knowledge-retriever-service.

Local fixtures only; the cross-service substrate harness (real Neo4j /
Postgres / Redis) is in ``tests/conftest.py`` at the repo root.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def key_free_embedder(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """This suite runs key-free, and says so OUT LOUD (#949).

    ``KRS_EMBEDDER``'s code default is ``openai`` since #949, so any search path resolves the
    calling organisation's model credential from the broker and REFUSES when there is none. These
    tests have no broker and want none: they are network-free by design, and a refusal would mask
    what each of them is actually about. Declaring ``hashing`` here is the explicit key-free
    selection the flip deliberately preserved, rather than leaving a suite-wide assumption to
    whatever the code default happens to be.

    Tests that are ABOUT the openai path set it themselves — the default-flip test deletes this
    variable in its own fixture, and the credential-refusal test overrides ``get_settings``
    outright — so both halves of the flip stay honestly pinned.

    The cached ``Settings`` is cleared on both sides so no test inherits another's environment.
    """
    from oraclous_knowledge_retriever_service.core.config import get_settings

    monkeypatch.setenv("KRS_EMBEDDER", "hashing")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
