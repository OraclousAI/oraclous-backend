"""#1109 (ruling item 3) — a search reaches for its ORG-DEFAULT model key, and the provider
REFUSES it — END-TO-END through the API GATEWAY (`:8006`). This must surface as
``MODEL_CREDENTIAL_REJECTED``, the SAME envelope
``test_intake_readback_gateway_e2e.py:230-233`` proves for the engine's #1108 sibling refusal —
never the generic ``MODEL_CREDENTIAL_REQUIRED`` the retriever has always used for "nothing
configured at all" (``core/dependencies.py``'s ``MODEL_CREDENTIAL_REQUIRED_DETAIL``).

Before #1109's ``[impl]`` lands, ``search_routes.py``'s ``_embedding_failed`` maps BOTH a rejected
key and an unconfigured one to the same ``MODEL_CREDENTIAL_REQUIRED`` refusal
(``retrieval_service.py``'s ``QueryEmbeddingCredentialRejected`` is caught but not distinguished),
so the ``MODEL_CREDENTIAL_REJECTED`` assertions below are RED until that split ships.

A real, syntactically-valid ``sk-or-v1-…`` OpenRouter key the provider will actually reject — never
a fake key against a fake harness, which would never distinguish "bad key" from "good key" at all
(mirrors #1108's ``test_a_key_the_provider_refuses_is_named_as_such``). No graph or ingested content
is created: ``get_retrieval_service`` resolves the query embedder from the org's credential at
DEPENDENCY-construction time, and ``RetrievalService.semantic``/``.hybrid`` embed the query
(``_embed_query``) BEFORE touching the repository at all (``retrieval_service.py``) — so the
provider rejection fires before any graph lookup, and an unseeded ``graph_id`` is enough to prove
it.

**Skip-guard.** ``_embedder_for_request`` (``core/dependencies.py``) only ever resolves a credential
— and so only ever reaches a provider — when ``KRS_EMBEDDER=openai``; the local/CI stack default is
``hashing``, which never constructs a broker client and never sees this refusal path at all. No
public gateway route exposes which embedder mode the retriever is running (this is a deployment
setting, not request-scoped data), so there is no honest live signal to probe for through the API —
this file gates on ``E2E_KRS_EMBEDDER=openai``, an env var the proof run itself must set. A SKIP
here proves nothing about the behaviour; it only means the check did not run. See #1109 plan §E.

Uses a FRESH registration per test: designating a bogus key as an org's default model credential
would break every OTHER search that org runs for the rest of the process (#724 caution, mirrored
from ``conftest.py``'s ``_designate_org_model_credential`` docstring), so no shared fixture user may
be reused here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_KRS_REAL_EMBEDDER = os.environ.get("E2E_KRS_EMBEDDER", "").strip() == "openai"
requires_real_embedder = pytest.mark.skipif(
    not _KRS_REAL_EMBEDDER,
    reason=(
        "set E2E_KRS_EMBEDDER=openai for this run to prove the refused-key path (the stack's own"
        " KRS_EMBEDDER must ALSO be openai) — the local/CI default is `hashing`, which never"
        " reaches a credential at all; a skip here is NOT a pass, see module docstring"
    ),
)

_SEMANTIC = "/v1/search/semantic"
_HYBRID = "/v1/search/hybrid"
_QUERY = "what did the founder say about pricing?"


def _designate_bogus_default_model_credential(c: httpx.Client, user_id: str) -> tuple[str, str]:
    """Store a syntactically-valid OpenRouter key the provider will REFUSE, and designate it this
    org's default model credential (#724) — the same two public calls a real operator makes,
    mirroring ``conftest.py``'s ``_designate_org_model_credential`` and #1108's readback proof.
    Returns ``(bogus_key, credential_id)``.
    """
    bogus_key = "sk-or-v1-" + uuid.uuid4().hex + uuid.uuid4().hex
    created = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": "e2e refused search key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": bogus_key},
        },
        timeout=20.0,
    )
    assert created.status_code == 201, f"credential create failed: {created.text}"
    credential_id = created.json()["id"]

    designated = c.put(
        f"/credentials/{credential_id}",
        json={
            "id": credential_id,
            "user_id": user_id,
            "tool_id": str(uuid.uuid4()),
            "provider": "openrouter",
            "cred_type": "api_key",
            "default_for": "model",
        },
        timeout=20.0,
    )
    assert designated.status_code == 200, f"designate default failed: {designated.text}"
    return bogus_key, credential_id


@requires_real_embedder
def test_semantic_search_with_a_refused_org_default_key_is_credential_rejected(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"searchrefused{uuid.uuid4().hex[:8]} user")
    c = gateway_client(user["token"])
    bogus_key, credential_id = _designate_bogus_default_model_credential(c, user["user_id"])

    resp = c.post(
        _SEMANTIC,
        json={"query": _QUERY, "graph_id": str(uuid.uuid4())},
        timeout=30.0,
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "MODEL_CREDENTIAL_REJECTED", resp.text
    assert bogus_key not in resp.text, resp.text
    assert credential_id not in resp.text, resp.text


@requires_real_embedder
def test_hybrid_search_with_a_refused_org_default_key_is_credential_rejected(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Sibling of the semantic proof above: hybrid fuses semantic + fulltext (RRF), and its embed
    call goes through the exact same ``_embed_query`` seam, so the same refusal must survive it
    too — checked cheaply, one more call on the same fresh registration and bogus credential."""
    user = register(f"hybridrefused{uuid.uuid4().hex[:8]} user")
    c = gateway_client(user["token"])
    bogus_key, credential_id = _designate_bogus_default_model_credential(c, user["user_id"])

    resp = c.post(
        _HYBRID,
        json={"query": _QUERY, "graph_id": str(uuid.uuid4())},
        timeout=30.0,
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "MODEL_CREDENTIAL_REJECTED", resp.text
    assert bogus_key not in resp.text, resp.text
    assert credential_id not in resp.text, resp.text
