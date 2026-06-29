"""#599 — a ``fan_out.over`` resolves to a real list END-TO-END through the gateway, both legs.

NO fakes (FUCK_CLAUDE rule 5): real registration → JWT → credential → engine → worker → LIVE harness
→ real OpenRouter. Two legs:

  LEG 1 (user input): the run is launched with ``inputs={"items": [3 nonces]}`` and a member
  ``fan_out.over: "$.items"`` — the member dispatches once per seeded item.

  LEG 2 (producer output): a REAL outliner member emits a JSON array as its harness output (the
  model genuinely writes ``["...","..."]``), and a downstream writer ``fan_out.over: "$.outliner"``
  PARSES that output and dispatches once per produced item — no hand-built list anywhere.

The assertion is the fan-out member's reduced result is a list whose length == the produced item
count: proof the fan-out actually expanded (pre-#599 it resolved to ``[]`` → zero dispatches).
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = "openrouter/openai/gpt-4o-mini"


def _model(cred_id: str) -> dict:
    return {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": cred_id},
    }


def _cred(c: httpx.Client, user: dict) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "byom",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _agents(files: dict[str, str]) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    for name, body in files.items():
        (adir / f"{name}.md").write_text(body)
    return root


def _import(root: pathlib.Path, user: dict, cred_id: str) -> tuple[dict, dict]:
    from oraclous_ohm.import_.setup import import_setup

    imported = import_setup(root, owner_organization_id=uuid.UUID(user["org_id"]), name="fanout")
    subs = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    for sub in subs.values():
        sub["models"] = [_model(cred_id)]
    doc = imported.manifest.model_dump(mode="json")
    doc["models"] = [_model(cred_id)]
    return doc, subs


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _run(c: httpx.Client, doc: dict, subs: dict, name: str, inputs: dict | None = None) -> dict:
    gid = c.post("/api/v1/graphs", json={"name": name}).json()["id"]
    body: dict = {"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid}
    if inputs is not None:
        body["inputs"] = inputs
    created = c.post("/v1/engine/team-runs", json=body)
    assert created.status_code == 202, created.text
    return _poll(c, created.json()["id"])


def _member(doc: dict, role: str) -> dict:
    return next(m for m in doc["members"] if m["role"] == role)


@requires_byom
def test_user_seeded_inputs_drive_a_fan_out(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"faninput{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    root = _agents(
        {"mapper": "---\nname: mapper\n---\nReply with the EXACT text you were given, verbatim.\n"}
    )
    doc, subs = _import(root, user, cred)
    # LEG 1: the member fans over a USER-SEEDED list — once per item.
    _member(doc, "mapper")["fan_out"] = {"over": "$.items", "reduce": "concat", "max_parallel": 4}
    items = [f"NONCE-{uuid.uuid4().hex[:6]}" for _ in range(3)]

    done = _run(c, doc, subs, "fanout-inputs", inputs={"items": items})
    assert done["state"] == "SUCCEEDED", f"the seeded fan-out must run — {done}"
    reduced = (done.get("results") or {}).get("mapper")
    assert isinstance(reduced, list), f"a concat fan-out yields a list — got {reduced!r}"
    assert len(reduced) == len(items), f"once per seeded item ({len(items)}) — got {len(reduced)}"


@requires_byom
def test_a_producer_json_array_drives_a_downstream_fan_out(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"fanprod{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    root = _agents(
        {
            # the outliner REALLY emits a JSON array as its harness output (no fake list anywhere)
            "outliner": "---\nname: outliner\n---\nOutput ONLY a JSON array of exactly three short "
            'distinct codenames, e.g. ["ALPHA","BRAVO","CHARLIE"], and nothing else.\n',
            "writer": "---\nname: writer\n---\nReply with the EXACT text you were given.\n",
        }
    )
    doc, subs = _import(root, user, cred)
    # LEG 2: the writer fans over the PRODUCER outliner's parsed list — once per produced item.
    w = _member(doc, "writer")
    w["depends_on"] = ["outliner"]
    w["fan_out"] = {"over": "$.outliner", "reduce": "concat", "max_parallel": 4}
    doc["runtime"]["entrypoint"] = "outliner"

    done = _run(c, doc, subs, "fanout-producer")
    assert done["state"] == "SUCCEEDED", f"the producer-driven fan-out must run — {done}"
    results = done.get("results") or {}
    # the outliner's REAL harness output carried the JSON array that drove the fan-out
    produced = json.loads(json.dumps(results.get("outliner")))
    raw = produced.get("output") if isinstance(produced, dict) else produced
    expected = json.loads(raw[raw.index("[") : raw.rindex("]") + 1])
    reduced = results.get("writer")
    assert isinstance(reduced, list), f"a concat fan-out yields a list — got {reduced!r}"
    assert len(reduced) == len(expected), (
        f"the writer fanned over the outliner's {len(expected)} produced items — got {len(reduced)}"
    )
