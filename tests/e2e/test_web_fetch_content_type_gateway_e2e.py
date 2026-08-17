"""WebFetch refuses binary content DEPLOYED-STACK proof through the API GATEWAY (#820).

A real user registers through the gateway (:8006), discovers the seeded **Web Research** tool,
instantiates it, brings their own credential via the public credentials API, and then points
``fetch`` and ``read`` at a **real PDF on the real internet**. The tool must refuse it with a typed
error naming the content type. Today it decodes the PDF as text and hands back mojibake.

Why a real URL rather than a fixture. The defect is a missing branch on a response header, and the
only honest way to prove the branch exists is to let a real server set the header. A stubbed
response would prove that our stub says ``application/pdf``.

Why one test and not three. Registration is the expensive call in this suite and the shared edge
rate limiter is per-IP (see the ``register`` fixture's own note in ``conftest.py``), so each extra
registered user raises the odds of a 429 somewhere else in the run. The three scenarios are one
user's session anyway: refuse the PDF on ``fetch``, refuse it on ``read``, still read an ordinary
HTML page. Each carries its own assertion message, so a failure still says which leg broke.

The credential bound here is a placeholder, and deliberately so. ``fetch``/``read`` are keyless in
the connector; only the instance-readiness check requires the mapping to exist. The user still
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
# it has sat at this path for years and is not behind a CDN that content-negotiates.
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


def test_web_fetch_refuses_a_real_pdf_but_still_reads_html_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: a real PDF URL comes back as a typed refusal, never as decoded binary."""
    user = register(f"wfpdf{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    iid = _ready_instance(c, user["user_id"])

    fetched = _execute(c, iid, "fetch", _PDF_URL)
    assert fetched["status"] == "FAILED", f"fetch decoded a PDF as text: {fetched}"
    assert fetched["error_type"] == "UNSUPPORTED_CONTENT_TYPE", fetched
    assert "application/pdf" in fetched["error_message"], fetched
    # The regression this issue exists to stop: the PDF file header reaching the caller as "text".
    assert "%PDF" not in json.dumps(fetched), fetched

    # `read` is the dangerous half: the HTML parser turns decoded binary into plausible prose.
    was_read = _execute(c, iid, "read", _PDF_URL)
    assert was_read["status"] == "FAILED", f"read parsed a PDF as HTML: {was_read}"
    assert was_read["error_type"] == "UNSUPPORTED_CONTENT_TYPE", was_read
    assert "%PDF" not in json.dumps(was_read), was_read

    # The gate narrows the surface without breaking the feature. A content-type check that refused
    # everything would satisfy both assertions above, so an ordinary page has to still come back.
    html = _execute(c, iid, "read", _HTML_URL)
    assert html["status"] == "SUCCESS", f"the gate refused an ordinary HTML page: {html}"
    assert "Example Domain" in html["output_data"]["text"], html
    # #820 criterion 3: truncation is visible in `data`, the only part the model receives.
    assert html["output_data"]["truncated"] is False, html
