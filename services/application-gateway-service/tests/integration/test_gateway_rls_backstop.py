"""Real-path proof of the application-gateway-service RLS SPLIT (ADR-030 / #353).

The gateway carved its DB access into an ORG-BOUND engine (the NOSUPERUSER ``oraclous_app`` role +
the org-GUC guard) and an OWNER engine for the two pre-auth PRODUCER reads. This suite drives the
**actual** service/repo paths the deployment wires (the repos build their engine through
``build_rls_engine`` / a guard-less owner engine exactly as the lifespan does), proving all four
load-bearing behaviours of the carve — and guarding the capability-registry/engine fail-closed bug
(an org-bound op that never binds the GUC reads zero rows + writes 42501 under the deployed
oraclous_app + FORCE'd RLS):

1. INTEGRATION-KEY AUTH still works under the split — ``IntegrationKeyAuthService.resolve`` over the
   OWNER-engine ``IntegrationKeyRepository`` resolves a key by its UNIQUE prefix to the org it
   PRODUCES, with NO org bound (the producer precedes org context). The key row was created in org
   A's bound scope on the org-bound engine, but the pre-auth resolve must find it cross-org —
   proving the owner engine bypasses RLS (the HARD RULE: a fail-close here breaks inbound key auth).

2. INBOUND WEBHOOK still works under the split — ``WebhookSubscriptionRepository.get_by_id`` on the
   OWNER engine resolves an inbound webhook's anchor (the id is its bearer-less credential) with NO
   org bound, finding a subscription created in org A's bound scope. A fail-close here breaks all
   inbound webhooks (the HARD RULE).

3. A TENANT sees its OWN rows — a real ``PublishedAgentService.publish`` + ``ChatService`` /
   ``ChatTurnService`` create/read-back on the org-bound engine SUCCEED (without the request-path
   binding the INSERT raises 42501 against the empty GUC) and the tenant reads them back. The chat
   message read (``chat_messages``, keyed by thread_id) also binds the org, so it returns the rows.

4. CROSS-ORG READ is empty + CROSS-ORG WRITE is denied — org B's org-bound reads never see org A's
   published agent / thread / key / subscription (RLS USING filters them, with the app ``WHERE``
   still in place AND at the data layer), and a write stamping org A while org B is bound raises
   SQLSTATE 42501 (the hard WITH CHECK). The backstop is intact.

Threats: T1-M1, T1-M3. ADR-006; ADR-012 §1a/§2; ADR-019; ADR-030 §3.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from oraclous_application_gateway_service.domain.integration_key import mint_key, prefix_of
from oraclous_application_gateway_service.repositories.chat_repository import ChatRepository
from oraclous_application_gateway_service.repositories.integration_key_repository import (
    IntegrationKeyRepository,
)
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)
from oraclous_application_gateway_service.repositories.webhook_subscription_repository import (
    WebhookSubscriptionRepository,
)
from oraclous_application_gateway_service.services.chat_service import ChatService
from oraclous_application_gateway_service.services.integration_key_auth_service import (
    IntegrationKeyAuthService,
)
from oraclous_application_gateway_service.services.published_agent_service import (
    PublishedAgentService,
)
from oraclous_governance import PrincipalType
from oraclous_substrate import org_scope
from sqlalchemy.exc import ProgrammingError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
    pytest.mark.isolation,
]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
async def repos(gateway_dsns) -> AsyncIterator[dict[str, object]]:  # noqa: ANN001
    """The REAL repos wired exactly as ``core/lifespan`` wires them: the org-bound repos on the
    NOSUPERUSER ``oraclous_app`` engine (the org-GUC guard installed by default), and the two
    OWNER-engine producer repos (``install_guard=False`` on the owner DSN) for ``get_by_prefix`` /
    ``get_by_id``. No org is bound by the test on the happy path — the repos bind it per op via
    ``org_scope`` (the fix under test).
    """
    owner_async, app_async = gateway_dsns
    bag = {
        # org-bound (oraclous_app + GUC guard)
        "agents": PublishedAgentRepository(app_async),
        "chat": ChatRepository(app_async),
        "keys": IntegrationKeyRepository(app_async),
        "subs": WebhookSubscriptionRepository(app_async),
        # owner engine (the two pre-auth producer reads)
        "keys_owner": IntegrationKeyRepository(owner_async, install_guard=False),
        "subs_owner": WebhookSubscriptionRepository(owner_async, install_guard=False),
    }
    try:
        yield bag
    finally:
        for repo in bag.values():
            await repo.close()  # type: ignore[attr-defined]


async def test_integration_key_auth_resolves_on_owner_engine(repos: dict[str, object]) -> None:
    """HARD RULE — integration-key auth survives the split. A key minted under org A (org-bound
    INSERT on oraclous_app, binding the GUC) is resolved by ``IntegrationKeyAuthService.resolve``
    over the OWNER-engine repo with NO org bound — the pre-auth ``get_by_prefix`` producer must read
    it cross-org (the owner bypasses RLS). The resolved principal carries org A's id."""
    keys: IntegrationKeyRepository = repos["keys"]  # type: ignore[assignment]
    keys_owner: IntegrationKeyRepository = repos["keys_owner"]  # type: ignore[assignment]

    minted = mint_key("oak")
    # the org-bound create binds org A via org_scope so the RLS WITH CHECK admits it (the bug repro:
    # without the binding this INSERT raises 42501 against the empty GUC). A capability allow-list
    # satisfies the exactly-one-binding check constraint.
    await keys.create(
        organisation_id=ORG_A,
        key_prefix=minted.key_prefix,
        key_hash=minted.key_hash,
        last4=minted.last4,
        capability_allow_list=["cap-x"],
    )

    # the pre-auth resolve runs on the OWNER engine with NO bound org (it PRECEDES org context).
    resolved = await IntegrationKeyAuthService(keys_owner).resolve(minted.plaintext)
    assert resolved.principal.organisation_id == ORG_A
    assert resolved.principal.principal_type == PrincipalType.SERVICE_ACCOUNT

    # control: the same producer read on the ORG-BOUND engine with no org bound is fail-closed (the
    # GUC is empty → RLS returns zero rows), which is exactly why get_by_prefix must use the owner
    # engine — a regression that points it at the org-bound engine would break inbound key auth.
    assert await keys.get_by_prefix(prefix_of(minted.plaintext)) is None


async def test_inbound_webhook_subscription_resolves_on_owner_engine(
    repos: dict[str, object],
) -> None:
    """HARD RULE — inbound webhooks survive the split. A subscription created under org A
    (org-bound) is resolved by ``get_by_id`` on the OWNER engine with NO org bound — the id is
    its bearer-less credential and must resolve cross-org (the owner bypasses RLS)."""
    subs: WebhookSubscriptionRepository = repos["subs"]  # type: ignore[assignment]
    subs_owner: WebhookSubscriptionRepository = repos["subs_owner"]  # type: ignore[assignment]

    created = await subs.create(
        organisation_id=ORG_A, target_slug="a-agent", broker_secret_ref=uuid.uuid4()
    )

    # the inbound webhook's pre-auth anchor resolve, on the OWNER engine, no bound org.
    resolved = await subs_owner.get_by_id(created.id)
    assert resolved is not None
    assert resolved.organisation_id == ORG_A
    assert resolved.target_slug == "a-agent"

    # control: the org-bound engine with no bound org fails closed (why get_by_id must be on owner).
    assert await subs.get_by_id(created.id) is None


async def test_tenant_published_agent_and_chat_roundtrip(repos: dict[str, object]) -> None:
    """A TENANT sees its OWN rows. A real ``PublishedAgentService.publish`` + ``ChatService`` on the
    org-bound engine SUCCEED (the request-path binding admits the INSERT) and org A reads its agent,
    thread, and the thread's messages back — the chat_messages read binds the org too."""
    agents: PublishedAgentRepository = repos["agents"]  # type: ignore[assignment]
    chat: ChatRepository = repos["chat"]  # type: ignore[assignment]
    agent_service = PublishedAgentService(agents)
    chat_service = ChatService(threads=chat, agents=agents)

    # publish (org-bound INSERT — binds org A via org_scope; without it, 42501)
    agent = await agent_service.publish(
        organisation_id=ORG_A, slug="a-agent", bound_capability_ref="capref-a"
    )
    assert agent.organisation_id == ORG_A

    # the tenant reads its own published agent back
    fetched = await agent_service.get_agent(organisation_id=ORG_A, slug="a-agent")
    assert fetched is not None and fetched.slug == "a-agent"
    assert [a.slug for a in await agent_service.list_agents(ORG_A)] == ["a-agent"]

    # start a thread + add a message (chat_threads + chat_messages, both org-bound), then read back
    thread = await chat_service.start_thread(
        organisation_id=ORG_A, user_id=USER_A, agent_slug="a-agent", title="t"
    )
    assert thread.organisation_id == ORG_A
    await chat.add_message(thread_id=thread.id, organisation_id=ORG_A, role="user", content="hello")
    # the message read (keyed by thread_id) binds org A so chat_messages RLS admits it
    msgs = await chat_service.get_messages(
        thread_id=thread.id, organisation_id=ORG_A, user_id=USER_A
    )
    assert msgs is not None and [m.content for m in msgs] == ["hello"]


async def test_cross_org_reads_are_empty(repos: dict[str, object]) -> None:
    """A TENANT never sees another org's rows through the org-bound engine. Org A creates an agent,
    a thread, a key and a subscription; org B's org-bound reads see none of them (RLS scopes the
    org-bound engine to the request-bound org, with the app WHERE in place AND at the data
    layer)."""
    agents: PublishedAgentRepository = repos["agents"]  # type: ignore[assignment]
    chat: ChatRepository = repos["chat"]  # type: ignore[assignment]
    keys: IntegrationKeyRepository = repos["keys"]  # type: ignore[assignment]
    subs: WebhookSubscriptionRepository = repos["subs"]  # type: ignore[assignment]
    agent_service = PublishedAgentService(agents)
    chat_service = ChatService(threads=chat, agents=agents)

    await agent_service.publish(
        organisation_id=ORG_A, slug="a-agent", bound_capability_ref="capref-a"
    )
    thread_a = await chat_service.start_thread(
        organisation_id=ORG_A, user_id=USER_A, agent_slug="a-agent", title="t"
    )
    minted = mint_key("oak")
    key_a = await keys.create(
        organisation_id=ORG_A,
        key_prefix=minted.key_prefix,
        key_hash=minted.key_hash,
        last4=minted.last4,
        capability_allow_list=["cap-x"],  # satisfies the exactly-one-binding check constraint
    )
    sub_a = await subs.create(
        organisation_id=ORG_A, target_slug="a-agent", broker_secret_ref=uuid.uuid4()
    )

    # org B sees NONE of org A's rows on the org-bound engine.
    assert await agent_service.get_agent(organisation_id=ORG_B, slug="a-agent") is None
    assert await agent_service.list_agents(ORG_B) == []
    assert (
        await chat_service.get_messages(
            thread_id=thread_a.id, organisation_id=ORG_B, user_id=USER_B
        )
        is None
    )
    assert await keys.get_for_org(key_id=key_a.id, organisation_id=ORG_B) is None
    assert await keys.list_for_org(ORG_B) == []
    assert await subs.list_for_org(ORG_B) == []

    # and org A still sees its OWN rows (isolation, not deletion).
    assert (await agent_service.get_agent(organisation_id=ORG_A, slug="a-agent")) is not None
    assert [r.id for r in await subs.list_for_org(ORG_A)] == [sub_a.id]


# Direct SQL against each table with NO organisation_id predicate — RLS is the only thing scoping
# it. The repos self-bind org_scope(row.org) on the happy path, so to prove the hard WITH CHECK
# bites we go RAW under a DELIBERATELY-mismatched org_scope (the begin-guard binds THAT org, the row
# stamps a different one), exactly as the data-layer isolation tests do. The GUC is bound the way
# the runtime binds it (org_scope → the begin-guard installed by build_rls_engine), never a WHERE.
_INSERT_AGENT = (
    "INSERT INTO published_agents (id, organisation_id, slug, bound_capability_ref, status) "
    "VALUES (:id, :org, :slug, 'capref', 'active')"
)
_INSERT_KEY = (
    "INSERT INTO integration_keys "
    "(id, organisation_id, key_prefix, key_hash, capability_allow_list, status) "
    "VALUES (:id, :org, :prefix, :h, '[\"cap-x\"]'::jsonb, 'active')"
)
_INSERT_MSG = (
    "INSERT INTO chat_messages (id, thread_id, organisation_id, role, content) "
    "VALUES (:id, :tid, :org, 'user', 'x')"
)


async def test_cross_org_write_is_denied(gateway_dsns) -> None:  # noqa: ANN001
    """The hard RLS WITH CHECK denies a write stamped for another org than the bound one (SQLSTATE
    42501), on each of the three writeable surfaces (published_agents, integration_keys,
    chat_messages — the table that rides along). The writes are RAW SQL on the org-bound engine
    under a deliberately-mismatched ``org_scope`` (NOT the repos' self-binding happy path), so the
    begin guard binds org B while the row stamps org A — proving the policy bites, not the app
    WHERE."""
    from oraclous_substrate import build_rls_engine
    from sqlalchemy import text

    _owner_async, app_async = gateway_dsns
    engine = build_rls_engine(app_async)  # the org-GUC guard installed, like the runtime repos
    try:
        for sql, params in (
            (_INSERT_AGENT, {"id": uuid.uuid4(), "org": ORG_A, "slug": "smuggled"}),
            (
                _INSERT_KEY,
                {"id": uuid.uuid4(), "org": ORG_A, "prefix": "deadbeefdeadbeef", "h": "h" * 64},
            ),
            (_INSERT_MSG, {"id": uuid.uuid4(), "tid": uuid.uuid4(), "org": ORG_A}),
        ):
            with pytest.raises(ProgrammingError) as exc:  # noqa: PT012 — bind + the write under it
                with org_scope(ORG_B):  # deliberately mismatched bound scope (NOT the happy path)
                    async with engine.begin() as conn:
                        await conn.execute(text(sql), params)
            assert getattr(exc.value.orig, "sqlstate", None) == "42501"
    finally:
        await engine.dispose()
