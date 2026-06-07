"""Integration: the edge guards are wired into the real app stack, and RequestId stays outermost so
a 413/429 from an inner guard still carries X-Request-Id (the middleware-order regression guard).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration


async def test_oversize_post_413_through_the_real_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "100")
    from oraclous_application_gateway_service.app.factory import create_app
    from oraclous_application_gateway_service.core.config import get_settings

    get_settings.cache_clear()
    # no lifespan -> app.state.redis is None -> the limiter fails open, exercising the size guard
    app = create_app(lifespan=None)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gw.test"
        ) as client:
            response = await client.post(
                "/v1/search",
                content=b"x" * 200,  # over the 100-byte cap
                headers={"authorization": "Bearer dev-token", "origin": "https://app.test"},
            )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert error["retryable"] is False
    assert set(error) == {"code", "message", "requestId", "retryable"}
    # RequestId is outermost, so the guard's 413 carries an X-Request-Id equal to the envelope's id
    assert response.headers.get("x-request-id") == error["requestId"]
    # CORS is OUTSIDE the guards, so a browser can actually read the 413 body
    assert response.headers.get("access-control-allow-origin") is not None
