"""#949 core (C7, KGS half) — `KGS_EMBEDDER` defaults to `openai`; `hashing` survives only as an
EXPLICIT choice for CI/offline work.

Mirrors this same service's own `test_credential_broker_failclosed.py` default-flip shape (#653):
"the unsafe state must not be the default" — here, the unsafe state is a workspace's write-side
silently staying on the keyless hashing embedder while an operator believes real embeddings are on
by default. The B5 regression (`make_embedder(settings)` dropping the resolved credential in
`similarity_pass.py`) that this flip exposes is pinned separately in
`test_recipe_similarity.py`, alongside the rest of that pass's tests.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.core.config import Settings

pytestmark = pytest.mark.unit

_ENV_VAR = "KGS_EMBEDDER"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV_VAR, raising=False)


def test_default_embedder_is_openai_not_hashing(clean_env: None) -> None:
    assert Settings().embedder == "openai"


def test_hashing_survives_as_an_explicit_opt_in(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV_VAR, "hashing")
    assert Settings().embedder == "hashing"


def test_openai_can_still_be_set_explicitly(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV_VAR, "openai")
    assert Settings().embedder == "openai"
