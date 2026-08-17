"""WebFetch refuses binary content DEPLOYED-STACK proof through the API GATEWAY (#820).

A real user registers through the gateway (:8006), discovers the seeded **Web Research** tool,
instantiates it, brings their own credential via the public credentials API, and then points
``fetch`` and ``read`` at a **real PDF on the real internet**. The tool must refuse it with a typed
error naming the content type. Today it decodes the PDF as text and hands back mojibake.

Why this needs a real URL rather than a fixture. The defect is a missing branch on a response
header, and the only honest way to prove the branch exists is to let a real server set the header.
A stubbed response would prove that our stub says ``application/pdf``.

The credential bound here is a placeholder, and deliberately so: ``fetch``/``read`` are keyless in
the connector: only the instance-readiness check requires the mapping to exist. The user still
stores it through the real ``POST /credentials/`` endpoint, so nothing is injected server-side
(FUCK_CLAUDE_FUCK_PAPERCLIP rule 5). No third party is faked, because no third party is called.

The package auto-skips when the gateway is down (conftest) — a skip is NOT a pass (rule 3).

RED until the content-type gate lands.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

# A long-lived W3C accessibility-test fixture served as `application/pdf`. Chosen for stability:
# it has been at this path for years and is not behind a CDN that content-negotiates.
_PDF_URL = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
_HTML_URL = "https://example.com"


def _ready_instance(c: httpx.Client, user_id: str) -> str:
    """Instantiate Web Research and satisfy the instance-readiness credential mapping."""
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "Web Research" in by_name, f"web-research not seeded; got {sorted(by_name)}"
    cap_id = by_name["Web Research"]["id"]

    inst = c.post(
        "/api/v1/instances",
        json={"capability_id": cap_id, "name": "web-fetch-ct", "configuration": {}, "settings": {}},
    )
    assert inst.status_code == 201, inst.text
    iid = inst.json()["id"]

    cred = c.post(
        "/credentials/",
        json={
            "tool_id": cap_id,
            "user_id": user_id,
            "name": "placeholder for the keyless fetch path",
            "provider": "tavily",
            "cred_type": "api_key",
            "credential": {"api_key": f"placeholder-{uuid.uuid4().hex}"},
        },
    )
    assert cred.status_code == 201, cred.text
    cfg = c.post(
        f"/api/v1/instances/{iid}/configure-credentials",
        json={"credential_mappings": {"api_key": cred.json()["id"]}},
    )
    assert cfg.status_code == 200, cfg.text
    return iid


def _execute(c: httpx.Client, iid: str, operation: str, url: str) -> dict:
    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": operation, "url": url}},
    )
    assert ex.status_code == 201, ex.text  # the dispatch succeeds; the outcome is in the body
    return ex.json()


def test_fetching_a_real_pdf_through_the_gateway_is_refused_not_decoded(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: a real PDF URL comes back as a typed refusal, never as decoded binary."""
    user = register(f"wfpdf{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    iid = _ready_instance(c, user["user_id"])

    out = _execute(c, iid, "fetch", _PDF_URL)

    assert out["status"] == "FAILED", out
    assert out["error_type"] == "UNSUPPORTED_CONTENT_TYPE", out
    assert "application/pdf" in out["error_message"], out
    # The regression this issue exists to stop: the PDF file header reaching the caller as "text".
    assert "%PDF" not in json.dumps(out), out


def test_reading_a_real_pdf_through_the_gateway_is_refused(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """`read` is the dangerous half: the HTML parser makes decoded binary look like prose."""
    user = register(f"wfpdfread{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    iid = _ready_instance(c, user["user_id"])

    out = _execute(c, iid, "read", _PDF_URL)

    assert out["status"] == "FAILED", out
    assert out["error_type"] == "UNSUPPORTED_CONTENT_TYPE", out


def test_an_html_page_still_reads_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The gate narrows the surface without breaking the feature.

    A content-type check that refused everything would pass both tests above. This pins that a
    normal HTML page still fetches, reads, and reports its truncation state to the caller.
    """
    user = register(f"wfhtml{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    iid = _ready_instance(c, user["user_id"])

    out = _execute(c, iid, "read", _HTML_URL)

    assert out["status"] == "SUCCESS", out
    assert "Example Domain" in out["output_data"]["text"], out
    # #820 criterion 3: truncation is visible in `data`, which is the only part the model receives.
    assert out["output_data"]["truncated"] is False, out
