"""Unit: the gateway publishes the OpenAPI v1 contract (ADR-015), serves it at the edge, and the
contract is internally consistent with the canonical ORA-37 error taxonomy.
"""

from __future__ import annotations

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from oraclous_application_gateway_service.app.factory import create_app
from oraclous_application_gateway_service.services.openapi_service import load_contract
from tools.contract.error_envelope import load_schema

pytestmark = pytest.mark.unit


def test_contract_loads_and_is_openapi_3() -> None:
    spec, text = load_contract("")
    assert str(spec["openapi"]).startswith("3."), spec["openapi"]
    assert spec["info"]["title"] == "Oraclous Platform API"
    assert yaml.safe_load(text)["openapi"] == spec["openapi"]


def test_error_envelope_component_matches_the_canonical_taxonomy() -> None:
    # the published spec's error-code enum must equal the cross-repo contract's enum, byte-for-byte
    spec, _ = load_contract("")
    published = spec["components"]["schemas"]["ErrorEnvelope"]["properties"]["error"]["properties"][
        "code"
    ]["enum"]
    canonical = load_schema()["properties"]["error"]["properties"]["code"]["enum"]
    assert published == canonical


def test_every_error_response_refs_the_envelope_schema() -> None:
    spec, _ = load_contract("")
    for name, resp in spec["components"]["responses"].items():
        if name == "UpstreamJson":  # the only non-error reusable response
            continue
        ref = resp["content"]["application/json"]["schema"]["$ref"]
        assert ref == "#/components/schemas/ErrorEnvelope", name


def test_no_internal_plane_disclosed() -> None:
    # the published contract is a deliberate disclosure surface — it must never expose an
    # /internal/* operation (the platform-internal plane is never edge-routed).
    spec, _ = load_contract("")
    assert not any(p.startswith("/internal") for p in spec.get("paths", {}))


async def test_openapi_json_served_at_the_edge_not_proxied() -> None:
    app = create_app(lifespan=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        r = await c.get("/v1/openapi.json")
    assert r.status_code == 200, r.text
    spec = r.json()
    assert str(spec["openapi"]).startswith("3.")
    # served by the edge route (the spec doc itself is not listed as an operation)
    assert "/v1/openapi.json" not in spec.get("paths", {})


async def test_docs_and_yaml_served() -> None:
    app = create_app(lifespan=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        docs = await c.get("/docs")
        spec_yaml = await c.get("/v1/openapi.yaml")
    assert docs.status_code == 200 and "swagger-ui" in docs.text.lower()
    assert spec_yaml.status_code == 200 and spec_yaml.text.lstrip().startswith("openapi:")


def test_fastapi_autospec_is_disabled() -> None:
    # we serve our own curated contract; FastAPI's auto-spec (which sees only /health + the
    # catch-all) is turned off so it cannot leak the `/{path:path}` proxy as a public operation.
    app = create_app(lifespan=None)
    assert app.openapi_url is None
    assert app.docs_url is None
