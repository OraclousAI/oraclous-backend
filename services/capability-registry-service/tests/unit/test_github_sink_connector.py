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

#1047 (owner ruling, 16 Sep) narrows the "configured, not passed" shape further: `repo` becomes
operator-configured ONLY. A call-supplied `repo` that differs from the bound instance configuration
is refused (`REPO_OVERRIDE_REFUSED`, no network) — defence in depth against a confused-deputy model
call, mirrored by the harness-side pre-dispatch refusal (#956 shape, tested elsewhere). A sink with
no configured `repo` fails closed (`REPO_NOT_CONFIGURED`) even when the call supplies one — a
call-supplied `repo` is never on its own legitimate. `_ctx()`/`_deliver()` below default to the
POST-#1047 shape (repo bound on configuration, never in the deliver input) so every pre-existing
test keeps asserting today's real behaviour; the repo-binding tests further down assert the new
edges directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit

_REPO = "octo/book"
#: a DIFFERENT repo than `_REPO`, for the override-refusal tests (#1047)
_OTHER_REPO = "acme/secret"


def _ctx(
    *, forge: str = "github", with_token: bool = True, repo: str | None = _REPO
) -> ExecutionContext:
    # the run binds the forge + (for gitea) GITHUB_API_BASE + (#1047) repo on the instance config;
    # repo=None models an instance with no configured repo at all.
    configuration: dict[str, str] = {"forge": forge}
    if repo is not None:
        configuration["repo"] = repo
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        configuration=configuration,
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
    # #1047: `repo` left the model-facing deliver input — it is bound on the instance
    # configuration via `_ctx(repo=...)`, never carried in the call by default any more.
    return {
        "operation": "deliver",
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


async def test_repo_can_be_bound_on_the_instance_configuration_not_the_input() -> None:
    """The instance can be CONFIGURED for a repo (the "configured, not passed" shape, #542, and —
    post-#1047 — the ONLY way a deliver ever reaches a repo at all): a deliver with no repo in the
    call falls back to the instance configuration's repo, and every forge call targets that bound
    repo."""
    seen: list[tuple[str, str]] = []
    res = await _sink(_forge_handler(seen)).execute(
        _deliver([{"path": "a.md", "content": "x"}]), _ctx()
    )
    assert res.success, res.error_message
    assert res.data["status"] == "DELIVERED"
    assert seen and all(_REPO in p for _, p in seen)  # every forge call hit the bound config repo


# --------------------------------------------------- repo binding is exclusive to config (#1047)


async def test_a_call_supplied_repo_that_differs_from_configuration_is_refused() -> None:
    """#1047 Q1 ruling: defence in depth at the connector (covers every caller, not only the
    harness's own pre-dispatch refusal). A call-supplied ``repo`` that differs from the bound
    instance configuration is refused before any network call, and the error never echoes either
    repo name verbatim — it must not help a caller enumerate which repos an instance can reach."""
    seen: list[tuple[str, str]] = []
    deliver = {**_deliver([{"path": "a.md", "content": "x"}]), "repo": _OTHER_REPO}
    res = await _sink(_forge_handler(seen)).execute(deliver, _ctx(repo=_REPO))
    assert not res.success
    assert res.error_type == "REPO_OVERRIDE_REFUSED"
    assert not seen, "a refused override must make zero forge calls"
    detail = res.error_message or ""
    assert _REPO not in detail
    assert _OTHER_REPO not in detail


async def test_a_call_supplied_repo_matching_configuration_is_accepted() -> None:
    """#1047 Q1 ruling: a call-supplied ``repo`` that MATCHES the bound configuration passes
    through unchanged — the refusal only trips on a mismatch, never on the mere presence of an
    explicit ``repo`` in the call."""
    seen: list[tuple[str, str]] = []
    deliver = {**_deliver([{"path": "a.md", "content": "x"}]), "repo": _REPO}
    res = await _sink(_forge_handler(seen)).execute(deliver, _ctx(repo=_REPO))
    assert res.success, res.error_message
    assert res.data["status"] == "DELIVERED"
    assert seen and all(_REPO in p for _, p in seen)


async def test_an_unconfigured_instance_refuses_a_call_supplied_repo() -> None:
    """#1047 Q2 ruling: ``repo`` is operator-configured only — a call-supplied ``repo`` is never
    legitimate on its own, so an instance with no configured repo fails closed even when the call
    supplies one, rather than falling back to it."""
    seen: list[tuple[str, str]] = []
    deliver = {**_deliver([{"path": "a.md", "content": "x"}]), "repo": _OTHER_REPO}
    res = await _sink(_forge_handler(seen)).execute(deliver, _ctx(repo=None))
    assert not res.success
    assert res.error_type == "REPO_NOT_CONFIGURED"
    assert not seen


async def test_an_unconfigured_instance_fails_closed_with_no_repo_at_all() -> None:
    """#1047 Q2 ruling, the baseline case: no configured repo and none supplied in the call."""
    seen: list[tuple[str, str]] = []
    res = await _sink(_forge_handler(seen)).execute(
        _deliver([{"path": "a.md", "content": "x"}]), _ctx(repo=None)
    )
    assert not res.success
    assert res.error_type == "REPO_NOT_CONFIGURED"
    assert not seen


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

    async def _deny(_url: str) -> str | None:  # #492: egress_allowed returns the pinned IP or None
        return None

    monkeypatch.setattr(sink_mod, "egress_allowed", _deny, raising=False)
    res = await _sink(lambda _r: httpx.Response(200, json={})).execute(
        _deliver([{"path": "a.md", "content": "x"}]), _ctx()
    )
    assert not res.success and res.error_type == "UNSAFE_URL"


async def test_the_sink_dials_the_pinned_ip_and_preserves_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #492 (review HIGH): the deliver-back connector must CONNECT to the resolved+vetted IP while
    # the Host header + TLS SNI stay the forge hostname — so a low-TTL rebind cannot re-point the
    # dial at an internal host between the vet and the connect. Every request in the multi-call
    # sequence must hit the pinned IP (never re-resolve the name).
    import oraclous_capability_registry_service.domain.connectors.github_sink as sink_mod

    async def _pin(_url: str) -> str | None:
        return "93.184.216.34"  # the pinned vetted IP for the forge hostname

    monkeypatch.setattr(sink_mod, "egress_allowed", _pin, raising=False)
    dialed: list[tuple[str, str | None, str | None]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        dialed.append((req.url.host, req.headers.get("host"), req.extensions.get("sni_hostname")))
        p, m = req.url.path, req.method
        if m == "POST" and p.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/deliver"})
        if m == "GET" and "/git/ref/heads/" in p:
            return httpx.Response(200, json={"object": {"sha": "basesha"}})
        if m == "GET" and "/contents/" in p:
            return httpx.Response(404, json={"message": "Not Found"})
        if m in ("PUT", "POST") and "/contents/" in p:
            return httpx.Response(201, json={"commit": {"sha": "c"}})
        if m == "POST" and p.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "https://forge/pr/1"})
        return httpx.Response(404, json={"message": "not found"})

    ctx = _ctx()
    ctx.configuration["base_url"] = "https://forge.example.com"  # a hostname → must be pinned
    res = await _sink(handler).execute(_deliver([{"path": "a.md", "content": "x"}]), ctx)
    assert res.success, res.error_message
    assert dialed, "the connector made no forge call"
    # EVERY call dialed the pinned IP; Host + SNI stayed the original forge hostname
    for host, host_header, sni in dialed:
        assert host == "93.184.216.34", (
            f"a call re-resolved the name instead of the pinned IP: {host}"
        )
        assert host_header == "forge.example.com"
        assert sni == "forge.example.com"


async def test_an_oversized_file_is_rejected() -> None:
    big = "z" * (2 * 1024 * 1024)  # 2 MiB > the per-file cap
    res = await _sink(lambda _r: httpx.Response(200, json={})).execute(
        _deliver([{"path": "huge.md", "content": big}]), _ctx()
    )
    assert not res.success and res.error_type == "INVALID_INPUT"
