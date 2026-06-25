"""Unit: the GitHubSinkConnector — the git-tree deliver-back sink (#515, E6 / O7).

The cloud "land in the user's source format" path: team outputs are written into the user's git tree
on a head branch + a PR (the book `.md`/`production/` files). Uses the **GitHub/Gitea-common
Contents API** (`PUT /contents/{path}` per changed file + `DELETE` + `POST /pulls`) so ONE connector
works identically against real github.com (#515a) and a local Gitea forge (deterministic proof) —
gitea has no low-level git-data write API (verified), so the Contents API is the common write path.
Branch creation diverges by forge (github `POST /git/refs` vs gitea `POST /branches`) → a small
forge-aware shim, selected by the bound `forge` config (default `github`).

A DISTINCT connector from the read-only `GitHubReader` (never widen the read tool → that would grant
write to every read-bound instance). PAT via the broker (`api_key`), egress-gated, fail-closed.
Oraclous owns the clean-delta/idempotency (`delivery_state`) — see test_delivery_delta.

RED until #515 [impl] lands `GitHubSinkConnector` + `GitHubSinkPlugin`. The not-yet-built seam is
imported FUNCTION-LOCALLY (§4.1) so collection stays green and only these tests fail at runtime.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit

_REPO = "octo/book"


def _ctx(*, forge: str = "github", with_token: bool = True) -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        # the run binds the forge + (for gitea) GITHUB_API_BASE on the instance config
        configuration={"forge": forge},
        # the broker-resolved shape: credentials["api_key"]["api_key"] (mirrors GitHubReader._token)
        credentials={"api_key": {"api_key": "ghp_dummy"}} if with_token else {},
    )


def _sink(handler: Callable[[httpx.Request], httpx.Response]):
    """A GitHubSinkConnector with an injected httpx MockTransport (no live forge)."""
    from oraclous_capability_registry_service.domain.connectors.github_sink import (
        GitHubSinkConnector,
    )

    ex = GitHubSinkConnector({"id": "x"})
    ex.transport = httpx.MockTransport(handler)
    return ex


def _forge_handler(seen: list[tuple[str, str]]) -> Callable[[httpx.Request], httpx.Response]:
    """A permissive Contents-API forge stand-in (both github + gitea shapes): records (method, path)
    and returns success for branch-create (git/refs OR branches), GET contents (404 → new file),
    PUT contents (commit), and POST pulls."""

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        p, m = req.url.path, req.method
        if m == "POST" and (
            p.endswith("/git/refs") or p.endswith("/branches")
        ):  # create head branch
            return httpx.Response(201, json={"ref": "refs/heads/deliver", "name": "deliver"})
        if m == "GET" and "/git/ref/heads/" in p:  # github: base sha for the new ref
            return httpx.Response(200, json={"object": {"sha": "basesha"}})
        if m == "GET" and "/contents/" in p:  # file does not exist yet → create (no sha needed)
            return httpx.Response(404, json={"message": "Not Found"})
        if m in ("PUT", "POST") and "/contents/" in p:  # write a file → one commit. github PUT
            # creates-or-updates; gitea's PUT is update-only (verified: a new-file PUT 422s
            # "[SHA]: Required"), so a NEW file on gitea is created via POST.
            return httpx.Response(201, json={"commit": {"sha": "filecommitsha"}})
        if m == "POST" and p.endswith("/pulls"):
            return httpx.Response(
                201, json={"number": 7, "html_url": f"https://forge/{_REPO}/pulls/7"}
            )
        return httpx.Response(404, json={"message": "not found"})

    return handler


def _deliver(files: list[dict]) -> dict:
    return {
        "operation": "deliver",
        "repo": _REPO,
        "base_branch": "main",
        "head_branch": "deliver/book",
        "files": files,
        "commit_message": "deliver book",
        "pr_title": "Book delivery",
        "pr_body": "automated",
    }


# ----------------------------------------------------------------- registration


def test_the_sink_plugin_is_registered_and_factory_resolvable() -> None:
    from oraclous_capability_registry_service.domain.connectors.github_sink import (
        GitHubSinkConnector,
    )
    from oraclous_capability_registry_service.domain.executors.factory import create_executor
    from oraclous_capability_registry_service.domain.plugins import plugin_registry
    from oraclous_capability_registry_service.domain.plugins.builtin import GitHubSinkPlugin

    ids = {p.plugin_id() for p in plugin_registry.discover()}
    assert GitHubSinkPlugin.plugin_id() in ids
    assert isinstance(create_executor(GitHubSinkPlugin().descriptor()), GitHubSinkConnector)


def test_the_sink_declares_a_required_github_pat_credential() -> None:
    from oraclous_capability_registry_service.domain.plugins.builtin import GitHubSinkPlugin

    reqs = GitHubSinkPlugin.CREDENTIAL_REQUIREMENTS
    assert any(r.get("type") == "api_key" and r.get("provider") == "github" for r in reqs)


# ------------------------------------------------------- deliver (happy path, both forges)


@pytest.mark.parametrize("forge", ["github", "gitea"])
async def test_deliver_writes_changed_files_via_contents_api_and_opens_a_pr(forge: str) -> None:
    seen: list[tuple[str, str]] = []
    res = await _sink(_forge_handler(seen)).execute(
        _deliver(
            [{"path": "bible/canon.md", "content": "X"}, {"path": "drafts/ch1.md", "content": "Y"}]
        ),
        _ctx(forge=forge),
    )
    assert res.success, res.error_message
    assert res.data["status"] == "DELIVERED"
    assert set(res.data["changed_paths"]) == {"bible/canon.md", "drafts/ch1.md"}
    assert res.data["pr_url"].endswith("/pulls/7")
    methods_paths = " ".join(f"{m}{p}" for m, p in seen)
    # a head branch was created (per-forge shim) + each file written via the Contents API + a PR
    assert ("/git/refs" in methods_paths) if forge == "github" else ("/branches" in methods_paths)
    # one Contents write per changed file — github PUT, gitea POST (its PUT is update-only, needs a
    # sha; a new file is created via POST — verified against real gitea 1.22).
    content_writes = sum(1 for m, p in seen if m in ("PUT", "POST") and "/contents/" in p)
    assert content_writes == 2
    assert "/pulls" in methods_paths


# ----------------------------------------------------------------- fail-closed


async def test_missing_pat_fails_closed_before_any_network() -> None:
    called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={})

    res = await _sink(handler).execute(
        _deliver([{"path": "a.md", "content": "x"}]), _ctx(with_token=False)
    )
    assert not res.success
    assert called["n"] == 0  # the missing credential is caught before any request


async def test_unknown_operation_is_rejected() -> None:
    res = await _sink(lambda _r: httpx.Response(200, json={})).execute(
        {"operation": "force_push", "repo": _REPO}, _ctx()
    )
    assert not res.success and res.error_type == "INVALID_OPERATION"


async def test_a_branch_create_conflict_fails_closed_never_force_writes() -> None:
    """A head branch that already diverged → branch-create 422s; sink fails closed (no force)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and (
            req.url.path.endswith("/git/refs") or req.url.path.endswith("/branches")
        ):
            return httpx.Response(
                422, json={"message": "branch already exists / not a fast forward"}
            )
        return httpx.Response(200, json={"object": {"sha": "s"}})

    res = await _sink(handler).execute(_deliver([{"path": "a.md", "content": "x"}]), _ctx())
    assert not res.success and res.error_type == "GIT_REF_CONFLICT"


async def test_an_unsafe_repo_base_is_egress_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sink takes an attacker-influenceable host surface → the egress SSRF gate guards it."""
    import oraclous_capability_registry_service.domain.connectors.github_sink as sink_mod

    async def _deny(_url: str) -> bool:
        return False

    monkeypatch.setattr(sink_mod, "egress_allowed", _deny, raising=False)
    res = await _sink(lambda _r: httpx.Response(200, json={})).execute(
        _deliver([{"path": "a.md", "content": "x"}]), _ctx()
    )
    assert not res.success and res.error_type == "UNSAFE_URL"


async def test_an_oversized_file_is_rejected() -> None:
    big = "z" * (2 * 1024 * 1024)  # 2 MiB > the per-file cap
    res = await _sink(lambda _r: httpx.Response(200, json={})).execute(
        _deliver([{"path": "huge.md", "content": big}]), _ctx()
    )
    assert not res.success and res.error_type == "INVALID_INPUT"
