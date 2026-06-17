"""Unit: the chat surface through the app — member-only auth + org/creating-member isolation.

Drives create_app with an in-memory chat repo (enforcing org+user scoping) + a published agent + the
dev USER bearer. The turn service is overridden (the harness call is in test_chat_turn_service);
this asserts the surface wiring, member-only gating, and that a thread is private to its creator.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_application_gateway_service.app.factory import create_app
from oraclous_application_gateway_service.core.dependencies import get_chat_turn_service
from oraclous_application_gateway_service.domain.integration_key import mint_key
from oraclous_application_gateway_service.schema.chat_schemas import ChatTurnOut

pytestmark = pytest.mark.unit

_DEV_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_DEV_USER = uuid.UUID("00000000-0000-0000-0000-0000000000e6")
_OTHER_USER = uuid.uuid4()
_DEV = {"authorization": "Bearer dev-token"}


class _FakeChatRepo:
    def __init__(self) -> None:
        self.threads: list = []
        self.messages: dict = {}

    async def create_thread(self, *, organisation_id, user_id, bound_agent_slug, title):  # noqa: ANN001
        t = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            created_by_user_id=user_id,
            bound_agent_slug=bound_agent_slug,
            title=title,
            deleted_at=None,
            last_message_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        self.threads.append(t)
        return t

    def _own(self, thread_id, org, user):  # noqa: ANN001
        return next(
            (
                t
                for t in self.threads
                if t.id == thread_id
                and t.organisation_id == org
                and t.created_by_user_id == user
                and t.deleted_at is None
            ),
            None,
        )

    async def get_thread(self, *, thread_id, organisation_id, user_id):  # noqa: ANN001
        return self._own(thread_id, organisation_id, user_id)

    async def list_threads(self, *, organisation_id, user_id, limit=100, offset=0):  # noqa: ANN001
        rows = [
            t
            for t in self.threads
            if t.organisation_id == organisation_id
            and t.created_by_user_id == user_id
            and t.deleted_at is None
        ]
        return rows[offset : offset + limit]

    async def soft_delete_thread(self, *, thread_id, organisation_id, user_id):  # noqa: ANN001
        t = self._own(thread_id, organisation_id, user_id)
        if t is None:
            return False
        t.deleted_at = datetime.now(UTC)
        return True

    async def list_messages(self, *, thread_id, organisation_id, limit=100, offset=0):  # noqa: ANN001
        return self.messages.get(thread_id, [])[offset : offset + limit]

    def add_assistant_message(self, *, thread_id):  # noqa: ANN001 — test helper
        msg = SimpleNamespace(
            id=uuid.uuid4(),
            role="assistant",
            content="ok",
            execution_id=uuid.uuid4(),
            total_tokens=3,
            rating=None,
            created_at=datetime.now(UTC),
        )
        self.messages.setdefault(thread_id, []).append(msg)
        return msg

    async def set_message_rating(self, *, thread_id, organisation_id, message_id, rating):  # noqa: ANN001
        for msg in self.messages.get(thread_id, []):
            if msg.id == message_id and msg.role == "assistant":
                msg.rating = rating
                return msg
        return None


class _FakeAgents:
    async def get_by_slug(self, *, organisation_id, slug):  # noqa: ANN001
        if slug == "weather" and organisation_id == _DEV_ORG:
            return SimpleNamespace(bound_capability_ref="cap-1", status="active")
        return None


class _FakeTurn:
    async def send_message(self, *, thread_id, content, principal):  # noqa: ANN001
        msg = SimpleNamespace(
            id=uuid.uuid4(),
            role="assistant",
            content="ok",
            execution_id=uuid.uuid4(),
            total_tokens=3,
            created_at=datetime.now(UTC),
        )
        from oraclous_application_gateway_service.schema.chat_schemas import MessageOut

        return ChatTurnOut(status="succeeded", message=MessageOut.model_validate(msg))


def _app():
    from oraclous_application_gateway_service.core.config import get_settings

    get_settings.cache_clear()
    app = create_app(lifespan=None)
    app.state.chat_repo = _FakeChatRepo()
    app.state.published_agent_repo = _FakeAgents()
    app.state.integration_key_repo = _FakeKeys()
    # the pre-auth get_by_prefix producer reads the OWNER-engine repo (ADR-030 §3); a fake has no
    # RLS so the same instance serves both.
    app.state.integration_key_owner_repo = app.state.integration_key_repo
    app.dependency_overrides[get_chat_turn_service] = _FakeTurn
    return app


class _FakeKeys:
    async def get_by_prefix(self, key_prefix):  # noqa: ANN001
        return self._row if hasattr(self, "_row") else None


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test")


async def test_start_thread_and_member_only() -> None:
    app = _app()
    async with _client(app) as c:
        r = await c.post("/v1/chat/threads", json={"agent_slug": "weather"}, headers=_DEV)
        assert r.status_code == 201 and r.json()["bound_agent_slug"] == "weather"
        # unknown agent -> 404
        bad = await c.post("/v1/chat/threads", json={"agent_slug": "nope"}, headers=_DEV)
        assert bad.status_code == 404
        # an integration-key bearer is rejected (member-only)
        minted = mint_key("oak")
        app.state.integration_key_repo._row = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=_DEV_ORG,
            key_prefix=minted.key_prefix,
            key_hash=minted.key_hash,
            status="active",
            expires_at=None,
            bound_agent_slug=None,
            capability_allow_list=None,
            cors_origins=None,
        )
        keyed = await c.post(
            "/v1/chat/threads",
            json={"agent_slug": "weather"},
            headers={"authorization": f"Bearer {minted.plaintext}"},
        )
        assert keyed.status_code == 403


async def test_send_message_and_transcript() -> None:
    app = _app()
    async with _client(app) as c:
        tid = (
            await c.post("/v1/chat/threads", json={"agent_slug": "weather"}, headers=_DEV)
        ).json()["id"]
        r = await c.post(f"/v1/chat/threads/{tid}/messages", json={"content": "hi"}, headers=_DEV)
        assert r.status_code == 200 and r.json()["status"] == "succeeded"


async def test_thread_is_private_to_its_creator() -> None:
    app = _app()
    # a thread created by ANOTHER user in the same org
    other = await app.state.chat_repo.create_thread(
        organisation_id=_DEV_ORG, user_id=_OTHER_USER, bound_agent_slug="weather", title="theirs"
    )
    async with _client(app) as c:
        # the dev user does not see it in their list
        mine = (await c.get("/v1/chat/threads", headers=_DEV)).json()
        assert all(t["id"] != str(other.id) for t in mine)
        # nor read its transcript / delete it -> 404 (not 403)
        assert (
            await c.get(f"/v1/chat/threads/{other.id}/messages", headers=_DEV)
        ).status_code == 404
        assert (await c.delete(f"/v1/chat/threads/{other.id}", headers=_DEV)).status_code == 404


async def test_soft_delete_then_gone() -> None:
    app = _app()
    async with _client(app) as c:
        tid = (
            await c.post("/v1/chat/threads", json={"agent_slug": "weather"}, headers=_DEV)
        ).json()["id"]
        assert (await c.delete(f"/v1/chat/threads/{tid}", headers=_DEV)).status_code == 204
        assert (await c.get(f"/v1/chat/threads/{tid}/messages", headers=_DEV)).status_code == 404
        assert all(t["id"] != tid for t in (await c.get("/v1/chat/threads", headers=_DEV)).json())


async def test_no_auth_is_401() -> None:
    app = _app()
    async with _client(app) as c:
        assert (await c.get("/v1/chat/threads")).status_code == 401


async def test_message_feedback_up_then_down_idempotent() -> None:
    app = _app()
    async with _client(app) as c:
        tid = (
            await c.post("/v1/chat/threads", json={"agent_slug": "weather"}, headers=_DEV)
        ).json()["id"]
        msg = app.state.chat_repo.add_assistant_message(thread_id=uuid.UUID(tid))
        up = await c.post(
            f"/v1/chat/threads/{tid}/messages/{msg.id}/feedback",
            json={"rating": "up"},
            headers=_DEV,
        )
        assert up.status_code == 200 and up.json()["rating"] == "up"
        # re-rating overwrites (idempotent surface)
        down = await c.post(
            f"/v1/chat/threads/{tid}/messages/{msg.id}/feedback",
            json={"rating": "down"},
            headers=_DEV,
        )
        assert down.status_code == 200 and down.json()["rating"] == "down"


async def test_message_feedback_rejects_bad_rating() -> None:
    app = _app()
    async with _client(app) as c:
        tid = (
            await c.post("/v1/chat/threads", json={"agent_slug": "weather"}, headers=_DEV)
        ).json()["id"]
        msg = app.state.chat_repo.add_assistant_message(thread_id=uuid.UUID(tid))
        bad = await c.post(
            f"/v1/chat/threads/{tid}/messages/{msg.id}/feedback",
            json={"rating": "meh"},
            headers=_DEV,
        )
        assert bad.status_code == 422


async def test_message_feedback_unknown_message_is_404() -> None:
    app = _app()
    async with _client(app) as c:
        tid = (
            await c.post("/v1/chat/threads", json={"agent_slug": "weather"}, headers=_DEV)
        ).json()["id"]
        missing = await c.post(
            f"/v1/chat/threads/{tid}/messages/{uuid.uuid4()}/feedback",
            json={"rating": "up"},
            headers=_DEV,
        )
        assert missing.status_code == 404


async def test_message_feedback_on_other_members_thread_is_404() -> None:
    app = _app()
    other = await app.state.chat_repo.create_thread(
        organisation_id=_DEV_ORG, user_id=_OTHER_USER, bound_agent_slug="weather", title="theirs"
    )
    msg = app.state.chat_repo.add_assistant_message(thread_id=other.id)
    async with _client(app) as c:
        denied = await c.post(
            f"/v1/chat/threads/{other.id}/messages/{msg.id}/feedback",
            json={"rating": "up"},
            headers=_DEV,
        )
        assert denied.status_code == 404


async def test_list_threads_pagination_window_and_backward_compat() -> None:
    # WP-10: limit/offset are OPTIONAL + honoured; the response stays a plain list (no envelope);
    # omitting both is backward-compatible (the small fixture set comes back whole).
    app = _app()
    for i in range(3):
        await app.state.chat_repo.create_thread(
            organisation_id=_DEV_ORG, user_id=_DEV_USER, bound_agent_slug="weather", title=f"t{i}"
        )
    async with _client(app) as c:
        all_threads = (await c.get("/v1/chat/threads", headers=_DEV)).json()
        assert (
            isinstance(all_threads, list) and len(all_threads) == 3
        )  # plain list, unchanged shape

        page = (await c.get("/v1/chat/threads?limit=2&offset=1", headers=_DEV)).json()
        assert isinstance(page, list) and len(page) == 2
        assert [t["id"] for t in page] == [t["id"] for t in all_threads[1:3]]


async def test_pagination_params_are_bounded() -> None:
    # the MAX page size + non-negative offset are enforced at the edge (422) -> no unbounded read
    app = _app()
    async with _client(app) as c:
        assert (await c.get("/v1/chat/threads?limit=201", headers=_DEV)).status_code == 422
        assert (await c.get("/v1/chat/threads?limit=0", headers=_DEV)).status_code == 422
        assert (await c.get("/v1/chat/threads?offset=-1", headers=_DEV)).status_code == 422
        assert (await c.get("/v1/chat/threads?limit=200", headers=_DEV)).status_code == 200
