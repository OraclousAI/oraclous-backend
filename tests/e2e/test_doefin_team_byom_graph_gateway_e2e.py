"""DoefinGPT end-to-end use-case proof (#543, ADR-041) — through the GATEWAY, on a REAL model.

The 3rd E6 use case, the canonical ADR-041 proof: a team's artifacts live on **Oraclous** (the
platform is the home, not a passthrough), graph-indexed + served — NOT shipped to github.

The whole loop, nothing injected, nothing mocked:
  1. IMPORT VIA THE GITHUB TOOL — the platform's GitHub Reader (the user's PAT, configured through
     the gateway) lists + reads ``.claude/agents`` from a remote repo; the importer assembles the
     Team Harness. (github is a tool that reads any provided remote repo — no client shortcut.)
  2. REAL MODEL (RULE 8) — every member's model points at the user's OpenRouter credential (stored
     via ``POST /credentials/``, broker-resolved); the harness runs LIVE. A per-run nonce is woven
     into every agent's prompt — only a real LLM can echo it; a fake-mode run CANNOT, so a green run
     is proof the model was real.
  3. ARTIFACTS ON ORACLOUS — members write in-loop (``Write`` → ``core/graph-ingest``) into the
     team's bound graph; the writes index (graph-indexed).
  4. SERVED — the outputs are listed + fetched verbatim through the unified ``/v1/artifacts`` route.

``byom``+``github``-marked → DESELECTED in CI (no real write PAT / model key there); run LOCALLY via
``scripts/e2e.sh --doefin`` with ``deploy/.env`` creds (GITHUB_IMPORT_PAT + GITHUB_IMPORT_REPO +
OPENROUTER_API_KEY) + the CTO's remote R4 check. Closes #543.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
import uuid
from collections.abc import Callable

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.byom, pytest.mark.github]

_PAT = os.environ.get("GITHUB_IMPORT_PAT")
_REPO = os.environ.get("GITHUB_IMPORT_REPO")
_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_doefin = pytest.mark.skipif(
    not (_PAT and _REPO and _OR_KEY),
    reason="GITHUB_IMPORT_PAT/REPO + OPENROUTER_API_KEY unset (the real-model doefin proof)",
)


def _cred(c: httpx.Client, user_id: str, provider: str, key: str, name: str) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": name,
            "provider": provider,
            "cred_type": "api_key",
            "credential": {"api_key": key},
        },
    )
    assert r.status_code == 201, r.text
    assert key not in r.text  # KMS-sealed — the secret is never echoed
    return r.json()["id"]


def _read_team_via_github_tool(c: httpx.Client, gh_cred: str) -> pathlib.Path:
    """The PLATFORM reads ``.claude/agents`` from the remote repo via the github tool (no clone)."""
    caps = {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": caps["GitHub Reader"]["id"],
            "name": f"gh-{uuid.uuid4().hex[:6]}",
            "configuration": {},
        },
    ).json()["id"]
    c.post(
        f"/api/v1/instances/{inst}/configure-credentials",
        json={"credential_mappings": {"api_key": gh_cred}},
    )
    listing = c.post(
        f"/api/v1/instances/{inst}/execute",
        json={"input_data": {"operation": "list_files", "repo": _REPO, "path": ".claude/agents"}},
    ).json()
    entries = (listing.get("output_data") or {}).get("entries") or []
    names = [
        e["name"] for e in entries if isinstance(e, dict) and e.get("name", "").endswith(".md")
    ]
    assert names, f"github tool listed no .claude/agents in {_REPO}: {listing}"
    tmp = pathlib.Path(tempfile.mkdtemp())
    adir = tmp / ".claude" / "agents"
    adir.mkdir(parents=True)
    for fn in names:
        out = c.post(
            f"/api/v1/instances/{inst}/execute",
            json={
                "input_data": {
                    "operation": "read_file",
                    "repo": _REPO,
                    "path": f".claude/agents/{fn}",
                }
            },
        ).json()
        (adir / fn).write_text((out.get("output_data") or {}).get("content") or "")
    return tmp


def _registry_capable(c: httpx.Client, sub: dict) -> list[dict]:
    """Keep only sub-harness capabilities the platform actually has (drop e.g. TodoWrite/Agent)."""
    import re

    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")

    reg = {_slug(x["name"]) for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    return [
        cap
        for cap in sub.get("capabilities", [])
        if (cap.get("ref", "").split("/")[-1].split("@")[0]) in reg
    ]


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


@requires_doefin
def test_doefin_team_imports_from_github_runs_on_real_model_and_serves_artifacts(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"doefin{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])

    # creds configured THROUGH the gateway (never injected)
    gh_cred = _cred(c, user["user_id"], "github", str(_PAT), "doefin github pat")
    or_cred = _cred(c, user["user_id"], "openrouter", str(_OR_KEY), "doefin openrouter key")

    # 1) the platform reads the team via the github tool; weave a per-run nonce into every agent's
    #    output — a SUCCEEDED run echoing it proves the LLM was REAL (fake mode cannot). Members
    #    execute their tools and write to the bound graph in-loop; a clean SUCCEEDED run (ADR-042,
    #    every member delivered, reached via re-run) lands + serves at least one artifact (below).
    nonce = uuid.uuid4().hex[:10]
    root = _read_team_via_github_tool(c, gh_cred)
    directive = f"\n\nIMPORTANT: include the exact token {nonce} verbatim in your output.\n"
    for md in (root / ".claude" / "agents").glob("*.md"):
        md.write_text(md.read_text() + directive)
    imported = import_setup(
        root, owner_organization_id=uuid.UUID(user["org_id"]), name="doefin-gpt", substrate="graph"
    )
    subs = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    assert len(subs) >= 10, f"expected the full doefin roster, got {len(subs)}"

    # 2) point EVERY member's model at the user's OpenRouter credential (cheap model) + keep only
    #    tools the platform has
    model = {
        "role": "primary",
        "binding": "openrouter/openai/gpt-4o-mini",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": or_cred},
    }
    for sub in subs.values():
        sub["models"] = [model]
        sub["capabilities"] = _registry_capable(c, sub)

    # 3) the bound graph (the team's workspace; artifacts land + index here)
    gid = c.post("/api/v1/graphs", json={"name": "doefin-gpt"}).json()["id"]

    # 4) run the team THROUGH THE GATEWAY on the REAL model
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": subs,
            "gate_decisions": {},
            "graph_id": gid,
        },
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    # ADR-042 (#551): a producing run is SUCCEEDED only when EVERY member delivered. A weak model
    # can fail/stall a member, but the run no longer ABORTS (the others still produce) and the
    # failed + blocked members are RE-RUNNABLE from the durable team state. Drive, then re-run the
    # failures until SUCCEEDED — bounded, so a genuinely stuck member surfaces (not a forever loop).
    done = _poll(c, run_id)
    for _ in range(4):
        if done["state"] == "SUCCEEDED":
            break
        assert done["state"] == "FAILED", done  # only a FAILED run is re-runnable (not PAUSED)
        rr = c.post(f"/v1/engine/team-runs/{run_id}/rerun")
        assert rr.status_code == 202, rr.text  # re-queued — the worker re-drives ONLY the failures
        done = _poll(c, run_id)
    assert done["state"] == "SUCCEEDED", f"run not SUCCEEDED after re-runs: {done}"

    # the ADR-042 verdict: SUCCEEDED iff EVERY member delivered — no member left "failed"/"blocked".
    member_status = done.get("member_status") or {}
    assert member_status, f"no per-member status recorded on a terminal run: {done}"
    assert not any(s in ("failed", "blocked") for s in member_status.values()), (
        f"a member did not deliver on a SUCCEEDED run: {member_status}"
    )
    assert len(done.get("results") or {}) == len(subs)  # every member produced a result
    # RULE 8: only a real LLM echoes the per-run nonce in its output — a fake-mode run cannot.
    assert nonce in str(done["results"]), (
        f"nonce {nonce!r} in no member result — was the harness LIVE? (fake = no proof)"
    )

    # 5) the team's outputs LIVE ON ORACLOUS, served verbatim through the unified /v1/artifacts
    #    surface for the bound graph. With the execution fixes (members execute their tools + the
    #    bound graph wins) the writes land + index; the clean-SUCCEEDED deliverable is at least one
    #    served artifact with real content (no longer best-effort).
    arts = c.get(f"/v1/artifacts?graph_id={gid}")
    assert arts.status_code == 200, arts.text
    served = [c.get(f"/v1/artifacts/{a['id']}").json() for a in arts.json()]
    # PROVENANCE — proven two ways, without over-constraining the served content: (a) the graph is
    # created FRESH per-run (gid above), so every served artifact is necessarily from THIS run, not
    # a stale one; (b) the per-run nonce is asserted in the run RESULTS (the members' final answers)
    # above, proving the model was REAL (RULE 8). Members persist a CURATED work-product (their
    # analysis docs) to the graph — NOT their nonce-bearing final answer — so the nonce is NOT in
    # the served content (verified empirically: a SUCCEEDED run serves 24 real artifacts, none echo
    # the token). The deliverable bar is therefore: ≥1 artifact landed + serves verbatim this run.
    assert any(b.get("content") for b in served), (
        f"no artifact landed + served on the bound graph for a SUCCEEDED run: {arts.json()}"
    )
