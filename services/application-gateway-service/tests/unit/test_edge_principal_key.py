"""Unit: get_edge_principal routes an oak-/oag- bearer to the key validator, degrades to 503 when
the store is down, maps a bad key to 401, and still accepts a JWT/dev bearer (additive branch)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from oraclous_application_gateway_service.core.dependencies import get_edge_principal
from oraclous_application_gateway_service.domain.integration_key import mint_key
from oraclous_governance import PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()


def _request(path: str, *, auth: str | None = None, repo=None):  # noqa: ANN001
    headers = {"authorization": auth} if auth else {}
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        headers=headers,
        state=SimpleNamespace(),  # request.state — where the resolved key is stashed (S4)
        app=SimpleNamespace(state=SimpleNamespace(integration_key_repo=repo)),
    )


class _FakeRepo:
    def __init__(self, row=None) -> None:
        self._row = row

    async def get_by_prefix(self, key_prefix):  # noqa: ANN001
        return self._row


def _row(minted):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        key_hash=minted.key_hash,
        status="active",
        expires_at=None,
        bound_agent_slug=None,
        capability_allow_list=None,
    )


async def test_valid_integration_key_resolves() -> None:
    minted = mint_key("oak")
    req = _request("/api/v1/tools", auth=f"Bearer {minted.plaintext}", repo=_FakeRepo(_row(minted)))
    principal = await get_edge_principal(req)
    assert principal is not None
    assert principal.principal_type == PrincipalType.SERVICE_ACCOUNT
    assert principal.organisation_id == _ORG


async def test_bad_integration_key_is_401() -> None:
    req = _request("/api/v1/tools", auth="Bearer oak-deadbeefdeadbeef-nope", repo=_FakeRepo(None))
    with pytest.raises(HTTPException) as exc:
        await get_edge_principal(req)
    assert exc.value.status_code == 401


async def test_store_down_is_503() -> None:
    # an oak- bearer with no repo wired (DB down) -> 503, NOT a crash or a silent allow
    minted = mint_key("oak")
    req = _request("/api/v1/tools", auth=f"Bearer {minted.plaintext}", repo=None)
    with pytest.raises(HTTPException) as exc:
        await get_edge_principal(req)
    assert exc.value.status_code == 503


class _RaisingRepo:
    async def get_by_prefix(self, key_prefix):  # noqa: ANN001
        from sqlalchemy.exc import OperationalError

        raise OperationalError("SELECT ...", {}, Exception("db down"))


async def test_db_error_mid_flight_is_503_not_500() -> None:
    # the DB drops after boot -> get_by_prefix raises -> the key path degrades to 503 (not a 500)
    minted = mint_key("oak")
    req = _request("/api/v1/tools", auth=f"Bearer {minted.plaintext}", repo=_RaisingRepo())
    with pytest.raises(HTTPException) as exc:
        await get_edge_principal(req)
    assert exc.value.status_code == 503


async def test_org_none_principal_is_refused() -> None:
    # belt-and-braces: a principal with no org must never be forwarded (unscoped-but-auth'd)
    minted = mint_key("oak")
    row = SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=None,
        key_hash=minted.key_hash,
        status="active",
        expires_at=None,
        bound_agent_slug=None,
        capability_allow_list=None,
    )
    req = _request("/api/v1/tools", auth=f"Bearer {minted.plaintext}", repo=_FakeRepo(row))
    with pytest.raises(HTTPException) as exc:
        await get_edge_principal(req)
    assert exc.value.status_code == 401


async def test_public_path_needs_no_auth() -> None:
    assert await get_edge_principal(_request("/v1/auth/login")) is None


async def test_jwt_dev_bearer_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    # a non-key bearer falls through to the existing dev/jwt path — the key branch is additive
    from oraclous_application_gateway_service.core.config import get_settings

    get_settings.cache_clear()
    req = _request("/api/v1/tools", auth="Bearer dev-token", repo=None)
    principal = await get_edge_principal(req)
    assert principal is not None and principal.principal_type == PrincipalType.USER
    get_settings.cache_clear()
