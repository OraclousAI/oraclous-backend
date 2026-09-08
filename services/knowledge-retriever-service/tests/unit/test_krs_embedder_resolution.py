"""#643 (C2) — knowledge-retriever-service resolves its QUERY embedder from `KRS_EMBEDDER` and the
organisation's OWN model credential, mirroring the already-proven `resolve_judge_for_org` shape
(ADR-037) and the KGS write-side `resolve_model_credential` resolution (#724).

Ruling on #949 Q1: embedding is a model call over customer content, so it is billed to the org's
own credential — there is NO platform-key fallback, matching every other site #724 already closed.

`resolve_embedder_for_org` and `services/credential_cache.py` do not exist on `main` yet. Every
seam import below is FUNCTION-LOCAL (`.claude/rules/tests-seam-imports.md`) so this file collects
cleanly and hard-fails RED with `ModuleNotFoundError`/`AttributeError` until the `[impl]` lands.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from oraclous_knowledge_retriever_service.core.config import Settings

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("a35472b5-9490-4e22-bf20-3399a5462f5a")


# --- KRS_EMBEDDER setting -----------------------------------------------------------------------


def test_krs_embedder_setting_accepts_hashing() -> None:
    assert Settings(embedder="hashing").embedder == "hashing"


def test_krs_embedder_setting_accepts_openai() -> None:
    assert Settings(embedder="openai").embedder == "openai"


def test_krs_embedder_setting_rejects_an_unknown_value() -> None:
    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
        Settings(embedder="not-a-real-mode")


# --- resolve_embedder_for_org: fake broker (no network) ------------------------------------------


class _FakeBrokerClient:
    """Stands in for `services.broker_client.BrokerClient`. Records every call so the caching
    test can assert a second resolution for the same org makes NO second network call."""

    instances: list[_FakeBrokerClient] = []

    def __init__(self, base_url: str, *, internal_key: str, **_kw: object) -> None:
        self.base_url = base_url
        self.internal_key = internal_key
        self.default_calls: list[tuple[uuid.UUID, str]] = []
        self.resolve_calls: list[tuple[str, uuid.UUID]] = []
        self.closed = False
        type(self).instances.append(self)

    async def org_default_credential_id(
        self, *, organisation_id: uuid.UUID, purpose: str = "model"
    ) -> str | None:
        self.default_calls.append((organisation_id, purpose))
        return getattr(self, "_default_id", None)

    async def resolve_credential(self, *, credential_id: str, organisation_id: uuid.UUID) -> dict:
        self.resolve_calls.append((credential_id, organisation_id))
        payload = getattr(self, "_payloads", {})
        if credential_id not in payload:
            from oraclous_knowledge_retriever_service.services.broker_client import (  # noqa: PLC0415, E501
                BrokerError,
            )

            raise BrokerError(f"credential {credential_id} not found")
        return payload[credential_id]

    async def aclose(self) -> None:
        self.closed = True


def _wire_broker(monkeypatch: pytest.MonkeyPatch, *, default_id: str | None, payloads: dict):
    """Patch `resolve_embedder_for_org`'s `BrokerClient` construction (mirrors how
    `resolve_judge_for_org`/`resolve_byom_judge` build one from settings) so no network is used."""
    from oraclous_knowledge_retriever_service.services import credential_cache  # noqa: PLC0415
    from oraclous_knowledge_retriever_service.services import (
        embedder as embedder_module,  # noqa: PLC0415, E501
    )

    credential_cache.clear()

    def _factory(base_url: str, *, internal_key: str, **kw: object) -> _FakeBrokerClient:
        client = _FakeBrokerClient(base_url, internal_key=internal_key, **kw)
        client._default_id = default_id  # type: ignore[attr-defined]
        client._payloads = payloads  # type: ignore[attr-defined]
        return client

    monkeypatch.setattr(embedder_module, "BrokerClient", _factory)
    return embedder_module


@pytest.fixture(autouse=True)
def _clean_broker_instances():
    _FakeBrokerClient.instances = []
    yield
    _FakeBrokerClient.instances = []


def _resolver():
    from oraclous_knowledge_retriever_service.services.embedder import (  # noqa: PLC0415
        resolve_embedder_for_org,
    )

    return resolve_embedder_for_org


async def test_hashing_mode_needs_no_broker_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key-free CI/dev path (#949's explicit-offline-selection carve-out) must not pay a
    broker round trip — it never even constructs a broker client."""
    _wire_broker(monkeypatch, default_id=None, payloads={})
    resolve = _resolver()

    embedder = await resolve(Settings(embedder="hashing"), organisation_id=_ORG)

    from oraclous_embedding import HashingEmbedder  # noqa: PLC0415

    assert isinstance(embedder, HashingEmbedder)
    assert _FakeBrokerClient.instances == []


async def test_openai_mode_resolves_the_org_default_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_broker(
        monkeypatch, default_id="org-default", payloads={"org-default": {"api_key": "sk-org"}}
    )
    resolve = _resolver()

    embedder = await resolve(Settings(embedder="openai"), organisation_id=_ORG)

    from oraclous_embedding import OpenAIEmbedder  # noqa: PLC0415

    assert isinstance(embedder, OpenAIEmbedder)
    (client,) = _FakeBrokerClient.instances
    assert client.default_calls == [(_ORG, "model")]
    assert client.resolve_calls == [("org-default", _ORG)]


async def test_the_credential_is_resolved_against_the_calling_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-008 / ORG001: the org is server-side context, so a credential belonging to another org
    can never be reached through this seam."""
    _wire_broker(
        monkeypatch, default_id="org-default", payloads={"org-default": {"api_key": "sk-org"}}
    )
    resolve = _resolver()

    await resolve(Settings(embedder="openai"), organisation_id=_ORG)

    (client,) = _FakeBrokerClient.instances
    assert client.default_calls[0][0] == _ORG
    assert client.resolve_calls[0][1] == _ORG


async def test_no_org_default_configured_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """#949 Q1 / #724: no platform-key fallback. Nothing configured -> refuse."""
    _wire_broker(monkeypatch, default_id=None, payloads={})
    resolve = _resolver()

    from oraclous_knowledge_retriever_service.services.broker_client import (  # noqa: PLC0415
        BrokerError,
    )

    with pytest.raises(BrokerError):
        await resolve(Settings(embedder="openai"), organisation_id=_ORG)


async def test_an_unresolvable_default_id_fails_closed_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_broker(monkeypatch, default_id="gone", payloads={})
    resolve = _resolver()

    from oraclous_knowledge_retriever_service.services.broker_client import (  # noqa: PLC0415
        BrokerError,
    )

    with pytest.raises(BrokerError):
        await resolve(Settings(embedder="openai"), organisation_id=_ORG)


def test_no_per_graph_credential_override_is_reachable_from_the_retriever() -> None:
    """Design call B3: the write side lets a graph pin its own credential
    (`knowledge_graphs.model_credential_id`), a column that lives in the graph service's own
    Postgres — the retriever cannot read it and must not reach across for it. The resolver's
    signature is therefore ONLY `(settings, *, organisation_id)`; a `graph_id` or
    `credential_id` parameter here would silently promise a per-graph override that C3's identity
    comparison, not this resolver, is what actually enforces (a differently-pinned graph refuses
    on mismatch rather than silently scoring)."""
    resolve = _resolver()
    params = set(inspect.signature(resolve).parameters)
    assert params == {"settings", "organisation_id"}


# --- credential caching: a second search for the same org must not re-hit the broker ------------


async def test_a_second_resolution_for_the_same_org_does_not_hit_the_broker_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this cache, every single search request pays a broker round trip before it can
    embed the query — a latency regression on the hottest path in the service (plan C2)."""
    _wire_broker(
        monkeypatch, default_id="org-default", payloads={"org-default": {"api_key": "sk-org"}}
    )
    resolve = _resolver()

    await resolve(Settings(embedder="openai"), organisation_id=_ORG)
    await resolve(Settings(embedder="openai"), organisation_id=_ORG)

    assert len(_FakeBrokerClient.instances) == 1, "a cache hit must not even build a new client"
    (client,) = _FakeBrokerClient.instances
    assert len(client.default_calls) == 1
    assert len(client.resolve_calls) == 1


async def test_a_different_org_is_not_served_from_the_first_orgs_cache_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_org = uuid.UUID("b46583c6-0501-5f33-c031-4400b6573fa1")
    _wire_broker(
        monkeypatch,
        default_id="org-default",
        payloads={"org-default": {"api_key": "sk-org"}},
    )
    resolve = _resolver()

    await resolve(Settings(embedder="openai"), organisation_id=_ORG)
    await resolve(Settings(embedder="openai"), organisation_id=other_org)

    assert len(_FakeBrokerClient.instances) == 2, "each org must be resolved independently"


# --- services/credential_cache.py: ported from the KGS module of the same name ------------------


def test_credential_cache_module_round_trips_a_default_id() -> None:
    from oraclous_knowledge_retriever_service.services import credential_cache  # noqa: PLC0415

    credential_cache.clear()
    assert credential_cache.get_default_id(_ORG, "model") is None
    credential_cache.put_default_id(_ORG, "model", "cred-1")
    assert credential_cache.get_default_id(_ORG, "model") == ("cred-1",)
    credential_cache.clear()


def test_credential_cache_invalidate_drops_only_the_named_org() -> None:
    from oraclous_knowledge_retriever_service.services import credential_cache  # noqa: PLC0415

    other = uuid.UUID("22222222-2222-2222-2222-222222222222")
    credential_cache.clear()
    credential_cache.put_default_id(_ORG, "model", "cred-1")
    credential_cache.put_default_id(other, "model", "cred-2")
    credential_cache.invalidate(_ORG)
    assert credential_cache.get_default_id(_ORG, "model") is None
    assert credential_cache.get_default_id(other, "model") == ("cred-2",)
    credential_cache.clear()
