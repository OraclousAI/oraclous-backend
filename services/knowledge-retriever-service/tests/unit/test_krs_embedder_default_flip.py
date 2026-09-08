"""#949 core (C7, KRS half) — `KRS_EMBEDDER` defaults to `openai`; `hashing` survives only as an
EXPLICIT choice (the owner's ruling note: "the ruling is that it stops being the silent default and
stops being served under a 'semantic' label, not that it is deleted").

Mirrors `services/knowledge-graph-service/tests/unit/test_credential_broker_failclosed.py`'s own
default-flip shape (#653), which pinned the same "unsafe state must not be the default" pattern for
`credential_broker_mode`.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_retriever_service.core.config import Settings

pytestmark = pytest.mark.unit

_ENV_VAR = "KRS_EMBEDDER"


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
