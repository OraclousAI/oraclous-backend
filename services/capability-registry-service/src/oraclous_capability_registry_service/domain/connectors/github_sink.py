"""GitHub/Gitea deliver-back sink connector (domain layer; #515, E6 / O7).

The cloud "land in the user's source format" path: team outputs are written into the user's git tree
on a head branch + a PR. Uses the GitHub/Gitea-common **Contents API** (`PUT /contents/{path}` per
changed file + `POST /pulls`) so ONE connector serves real github.com (#542) and a local Gitea
forge (the deterministic O7 proof) — gitea has no low-level git-data write API. Branch creation
diverges by forge (github `POST /git/refs` over git-data refs; gitea `POST /branches`) → a small
forge-aware shim by the bound ``forge`` config. A DISTINCT connector from the read-only
``GitHubReader`` (never widen the read tool → that would grant write to every read-bound instance).

Oraclous owns the clean-delta/idempotency: the injected ``delivery_repo`` supplies the last-written
hashes (compute the minimal diff; an identical re-deliver is a NO_OP) and records the new state. PAT
via the broker (never server-injected); egress-gated; fail-closed. The forge is only the executor.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import httpx

from oraclous_capability_registry_service.domain.delivery import (
    changed_paths,
    content_hash,
    delivery_key,
)
from oraclous_capability_registry_service.domain.egress import egress_allowed
from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

if TYPE_CHECKING:
    from oraclous_capability_registry_service.repositories.delivery_state_repository import (
        DeliveryStateRepository,
    )

_GITHUB_BASE = "https://api.github.com"
_MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MiB per delivered file
_TIMEOUT_S = 35.0  # a multi-call Contents sequence > the 30s default


class GitHubSinkConnector(InternalTool):
    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None
    #: the clean-delta store, injected on the LIVE path (None on the unit path → first-deliver-all)
    delivery_repo: DeliveryStateRepository | None = None

    def _token(self, context: ExecutionContext) -> str | None:
        creds = self.get_credentials(context, "api_key")
        if not creds or not creds.get("api_key"):
            return None
        return str(creds["api_key"])

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        # fail-closed gates first, before any network
        if input_data.get("operation") != "deliver":
            return ExecutionResult(
                success=False, error_message="unsupported operation", error_type="INVALID_OPERATION"
            )
        token = self._token(context)
        if token is None:
            return ExecutionResult(
                success=False,
                error_message="a github/gitea api_key (PAT) credential is required",
                error_type="INVALID_INPUT",
            )
        # repo may be bound on the instance configuration (the sink IS for that repo — the
        # "configured, not passed" shape, #542) or passed per deliver; configuration takes the bind.
        repo = input_data.get("repo") or context.configuration.get("repo")
        if not isinstance(repo, str) or not repo:
            return ExecutionResult(
                success=False,
                error_message="'repo' is required (deliver input or instance configuration)",
                error_type="INVALID_INPUT",
            )
        files = input_data.get("files") or []
        for f in files:
            if len(str(f.get("content", "")).encode("utf-8")) > _MAX_FILE_BYTES:
                return ExecutionResult(
                    success=False,
                    error_message="a delivered file exceeds the size cap",
                    error_type="INVALID_INPUT",
                )
        base_branch = input_data.get("base_branch", "main")
        head_branch = input_data.get("head_branch") or base_branch
        forge = context.configuration.get("forge", "github")
        base_url = str(context.configuration.get("base_url") or _GITHUB_BASE).rstrip("/")
        if not await egress_allowed(base_url):
            return ExecutionResult(
                success=False,
                error_message="the forge URL is not an allowed target",
                error_type="UNSAFE_URL",
            )

        # the clean delta — decided BEFORE any forge call (NO_OP short-circuits with no network)
        incoming = {f["path"]: content_hash(str(f["content"]).encode("utf-8")) for f in files}
        org_id = context.organisation_id
        stored = (
            await self.delivery_repo.get_hashes(organisation_id=org_id, repo=repo, ref=head_branch)
            if self.delivery_repo is not None
            else {}
        )
        changed = changed_paths(incoming, stored)
        if not changed:
            return ExecutionResult(
                success=True,
                data={
                    "status": "NO_OP",
                    "changed_paths": [],
                    "pr_url": None,
                    "branch": head_branch,
                },
            )

        # a prior delivery to this (org, repo, head) means the head branch + PR already exist (the
        # recurring-refresh case) → write the diff to the existing branch, never re-create it. Only
        # the FIRST delivery creates the branch + PR; a create conflict THERE is a genuine GIT_REF
        # conflict (fail closed). Keyed on delivery_state, so the unit branch-conflict test (no
        # injected repo → no prior state → first delivery) still fails closed as specified.
        redeliver = bool(stored)
        content_by_path = {f["path"]: str(f["content"]) for f in files}
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=_TIMEOUT_S,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            if not redeliver:
                conflict = await self._ensure_branch(client, forge, repo, base_branch, head_branch)
                if conflict is not None:
                    return conflict
            for path in changed:
                body: dict[str, Any] = {
                    "message": input_data.get("commit_message", "deliver"),
                    "branch": head_branch,
                    "content": base64.b64encode(content_by_path[path].encode("utf-8")).decode(
                        "ascii"
                    ),
                }
                existing = await self._existing_sha(client, repo, path, head_branch)
                if existing:  # an existing file → the Contents API requires its blob sha to update
                    body["sha"] = existing
                # github's PUT /contents creates OR updates; gitea's PUT is update-only (requires a
                # sha — verified: a new-file PUT 422s "[SHA]: Required"), so a NEW file on gitea is
                # created via POST. Update (sha present) is PUT on both forges.
                method = "POST" if (forge == "gitea" and not existing) else "PUT"
                write = await client.request(method, f"/repos/{repo}/contents/{path}", json=body)
                if write.status_code not in (200, 201):
                    return self._forge_error(write.status_code, f"writing {path}")
            if redeliver:
                # the PR for head→base was opened by the first delivery; reuse it (best-effort) —
                # opening a second would conflict, and the diff lands on the same branch regardless.
                pr_url = await self._existing_pr_url(client, repo, head_branch)
            else:
                pr = await client.post(
                    f"/repos/{repo}/pulls",
                    json={
                        "title": input_data.get("pr_title", "Delivery"),
                        "body": input_data.get("pr_body", ""),
                        "head": head_branch,
                        "base": base_branch,
                    },
                )
                if pr.status_code not in (200, 201):
                    return self._forge_error(pr.status_code, "opening the PR")
                pr_url = pr.json().get("html_url")

        # record the new state so the NEXT delivery diffs against it (recurring-refresh clean delta)
        if self.delivery_repo is not None:
            await self.delivery_repo.record(
                organisation_id=org_id,
                repo=repo,
                ref=head_branch,
                file_hashes=incoming,
                delivery_key=delivery_key(
                    organisation_id=org_id, repo=repo, ref=head_branch, file_hashes=incoming
                ),
            )
        return ExecutionResult(
            success=True,
            data={
                "status": "DELIVERED",
                "changed_paths": changed,
                "pr_url": pr_url,
                "branch": head_branch,
            },
        )

    async def _ensure_branch(
        self, client: httpx.AsyncClient, forge: str, repo: str, base: str, head: str
    ) -> ExecutionResult | None:
        """Create the head branch off base (the one place github/gitea diverge). None = created."""
        if forge == "gitea":
            r = await client.post(
                f"/repos/{repo}/branches", json={"new_branch_name": head, "old_branch_name": base}
            )
        else:  # github: git-data refs (gitea has no git-data write API)
            ref = await client.get(f"/repos/{repo}/git/ref/heads/{base}")
            if ref.status_code != 200:
                return self._forge_error(ref.status_code, "resolving the base branch")
            sha = (ref.json().get("object") or {}).get("sha")
            r = await client.post(
                f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{head}", "sha": sha}
            )
        if r.status_code in (200, 201):
            return None
        # the head branch already exists / has diverged → fail closed, NEVER force-write
        return ExecutionResult(
            success=False,
            error_message="the head branch could not be created (it may already exist / diverged)",
            error_type="GIT_REF_CONFLICT",
            metadata={"status_code": r.status_code},
        )

    @staticmethod
    async def _existing_sha(
        client: httpx.AsyncClient, repo: str, path: str, ref: str
    ) -> str | None:
        r = await client.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if r.status_code == 200 and isinstance(r.json(), dict):
            return r.json().get("sha")
        return None

    @staticmethod
    async def _existing_pr_url(client: httpx.AsyncClient, repo: str, head: str) -> str | None:
        """The html_url of the open PR whose head is ``head`` (github + gitea common shape), else
        None — a re-deliver reuses the first delivery's PR rather than opening a duplicate."""
        r = await client.get(f"/repos/{repo}/pulls", params={"state": "open"})
        if r.status_code == 200 and isinstance(r.json(), list):
            for pr in r.json():
                if (pr.get("head") or {}).get("ref") == head:
                    return pr.get("html_url")
        return None

    @staticmethod
    def _forge_error(status_code: int, doing: str) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            error_message=f"the forge returned {status_code} {doing}",
            error_type="FORGE_HTTP_ERROR",
            metadata={"status_code": status_code},
        )
