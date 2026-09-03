"""#664 — the credential pre-flight the GO sheet promises actually runs, at create, before spend.

The GO sheet says, above the button: "Tool credentials are checked at run time — a missing one
stops the run with a connect prompt, never a silent failure." On team run ``c904fade`` the
``github-reader`` member's instance was ``CONFIGURATION_REQUIRED``; no prompt appeared, the run
started, six tool calls took six 409s, and 5,800 real tokens were spent before it failed. Today
``TeamRunService.create`` validates the manifest, the ceilings, the filed agents, the inputs and
the graph id — and never looks at a tool instance. Tool→instance binding happens in the harness,
per member, after the worker has already dispatched.

The contract these tests pin (RED until the [impl] lands):

* ``TeamRunService.create`` runs ``_preflight_tool_credentials`` after the member manifests are
  resolved and BEFORE any row is written or enqueued. For every member that declares tools it
  takes the resolved sub-harness ``capabilities[]``, maps each ``ref`` to the org's registered
  tool (``RegistryClient.list_tools``, the ``GET /api/v1/tools`` rows the harness itself resolves
  against), lists the org's instances (``list_instances``) and asks the registry's own readiness
  verdict for the capability's instances (``validate_execution`` →
  ``GET /api/v1/instances/{id}/validate-execution``) until one answers ``is_ready``. That verdict
  is what makes ``CONFIGURATION_REQUIRED`` (a mapped-but-unresolvable or revoked credential, an
  OAuth grant that lapsed) count as missing — criterion 3 — without re-deriving the registry's
  status rules here.
* A binding with no ready instance raises ``TeamRunPreflightError`` (a ``TeamRunError``, status
  409, ``error_type="tool_not_configured"``) carrying ``missing`` — one ``{role, binding,
  credential_type}`` per unmet binding — and ``needs_credential`` = ``{"requirement_id":
  <credential_type>, "provider": <binding slug>}`` for the FIRST miss: the leak-safe pair the
  gateway already relays as CREDENTIALS_REQUIRED and the console already renders as a connect
  prompt. Nothing is persisted, nothing is enqueued, so nothing can spend.
* A team whose members declare no tools makes no registry call at all (criterion 4). A binding
  whose sub-harness carries its own ``config.credential_mappings`` is skipped — the harness mints
  and configures a fresh instance for it, so refusing would be a false negative. An unreachable
  registry is a 502 ``registry_unavailable`` (fail closed, the #695 posture), never a skip. With no
  registry wired at all the check is not run — the ``graphs`` precedent — and the harness gate
  (#663) remains the second line.

New-symbol imports are function-local (§4.1) so collection never breaks other suites.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.registry_client import RegistryClientError
from oraclous_execution_engine_service.services.team_run_service import TeamRunError, TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_READER_ID = uuid.uuid4()
_SEARCH_ID = uuid.uuid4()


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


class _RunRow:
    def __init__(self, **kw: Any) -> None:
        self.id = uuid.uuid4()
        self.organisation_id = kw["organisation_id"]
        self.user_id = kw["user_id"]
        self.manifest = kw["manifest"]
        self.sub_harnesses = kw["sub_harnesses"]
        self.state = "QUEUED"
        self.results: dict[str, Any] = {}
        self.graph_id = kw.get("graph_id")
        self.workspace_root = kw.get("workspace_root")
        self.inputs = kw.get("inputs")
        self.seed_from_run_id = kw.get("seed_from_run_id")
        self.gate_decisions = kw.get("gate_decisions")


class _FakeRunRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, _RunRow] = {}

    async def create(self, **kw: Any) -> _RunRow:
        row = _RunRow(**kw)
        self.rows[row.id] = row
        return row


def _tool(capability_id: uuid.UUID, name: str, *, credential_type: str = "api_key") -> dict:
    """A ``GET /api/v1/tools`` row: the registry's own name + the descriptor's requirements."""
    return {
        "id": str(capability_id),
        "name": name,
        "kind": "tool",
        "status": "active",
        "descriptor": {
            "spec": {
                "credential_requirements": [{"type": credential_type, "required": True}],
            }
        },
    }


def _instance(
    capability_id: uuid.UUID,
    *,
    status: str,
    mappings: dict[str, str] | None = None,
    required: list[str] | None = None,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "capability_id": str(capability_id),
        "name": "github-reader",
        "status": status,
        "credential_mappings": mappings or {},
        "required_credentials": required or ["api_key"],
    }


class _FakeRegistry:
    """The registry seam the pre-flight reads: tools, instances, and the readiness verdict."""

    def __init__(
        self,
        *,
        tools: list[dict] | None = None,
        instances: list[dict] | None = None,
        unreachable: bool = False,
    ) -> None:
        self.tools = tools or []
        self.instances = instances or []
        self.unreachable = unreachable
        self.calls: list[str] = []

    async def list_tools(self) -> list[dict[str, Any]]:
        self.calls.append("list_tools")
        if self.unreachable:
            raise RegistryClientError("registry unreachable: ConnectError")
        return list(self.tools)

    async def list_instances(self) -> list[dict[str, Any]]:
        self.calls.append("list_instances")
        if self.unreachable:
            raise RegistryClientError("registry unreachable: ConnectError")
        return list(self.instances)

    async def validate_execution(self, instance_id: uuid.UUID) -> dict[str, Any]:
        """``GET /api/v1/instances/{id}/validate-execution`` — the registry's ValidationReport.
        ``READY`` rows are ready; anything else reports a CREDENTIAL_NOT_CONFIGURED error naming
        the first unmapped required type (what the real service derives from the mappings)."""
        self.calls.append(f"validate:{instance_id}")
        row = next(i for i in self.instances if i["id"] == str(instance_id))
        if row["status"] == "READY":
            return {
                "is_ready": True,
                "instance_id": str(instance_id),
                "status": "READY",
                "checks": {"capability": "passed", "credentials": "passed"},
                "errors": [],
                "action_items": [],
            }
        missing = [t for t in row["required_credentials"] if t not in row["credential_mappings"]]
        return {
            "is_ready": False,
            "instance_id": str(instance_id),
            "status": "CONFIGURATION_REQUIRED",
            "checks": {"capability": "passed", "credentials": "failed"},
            "errors": [
                {
                    "type": "CREDENTIAL_NOT_CONFIGURED",
                    "message": f"required credential '{t}' is not configured",
                    "severity": "critical",
                    "credential_type": t,
                }
                for t in (missing or row["required_credentials"][:1])
            ],
            "action_items": [],
        }

    async def get_capability(self, capability_id: uuid.UUID) -> dict[str, Any] | None:
        return None  # every member here carries an INLINE sub-harness; nothing to resolve (#695)


def _service(registry: Any) -> tuple[TeamRunService, _FakeRunRepo, list[uuid.UUID]]:
    repo = _FakeRunRepo()
    enqueued: list[uuid.UUID] = []
    svc = TeamRunService(
        team_runs=repo,  # type: ignore[arg-type] — duck-typed seam in unit tests
        enqueue=lambda rid, _org, _user: enqueued.append(rid),
        registry=registry,
    )
    return svc, repo, enqueued


def _team(members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "team",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"]},
    }


def _agent(role: str, tools: list[str] | None = None) -> dict[str, Any]:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": [],
        "tools": tools or [],
        "outputs_schema": {"required": ["summary"]},
    }


def _sub(role: str, capabilities: list[dict[str, Any]]) -> dict[str, Any]:
    """An inline single-agent sub-harness — the compiled-team shape (member ``tools[]`` = ceiling,
    sub-harness ``capabilities[]`` = grant, #659)."""
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": role, "owner_organization_id": str(_ORG)},
        "capabilities": capabilities,
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }


def _reader_cap(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cap: dict[str, Any] = {"ref": "core/github-reader@1.0.0", "binding": "github-reader"}
    if config is not None:
        cap["config"] = config
    return cap


async def _go(svc: TeamRunService, manifest: dict, subs: dict[str, dict]) -> Any:
    return await svc.create(_principal(), manifest=manifest, sub_harnesses=subs, gate_decisions={})


# ── the run that must not start ────────────────────────────────────────────────────────────────


async def test_regression_run_c904fade_an_unconfigured_instance_stops_the_run_at_zero_tokens() -> (
    None
):
    """The org's only ``github-reader`` instance is CONFIGURATION_REQUIRED (no credential mapped).
    Pressing GO must NOT create a run: no row, nothing enqueued, so no worker, no harness, no
    model tokens. The error names the capability and the credential type — the connect prompt."""
    from oraclous_execution_engine_service.services.team_run_service import TeamRunPreflightError

    registry = _FakeRegistry(
        tools=[_tool(_READER_ID, "GitHub Reader")],
        instances=[_instance(_READER_ID, status="CONFIGURATION_REQUIRED")],
    )
    svc, repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    with pytest.raises(TeamRunPreflightError) as exc:
        await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert exc.value.status_code == 409
    assert exc.value.error_type == "tool_not_configured"
    assert isinstance(exc.value, TeamRunError)  # the route's existing mapping still applies
    assert exc.value.missing == [
        {"role": "fetcher", "binding": "github-reader", "credential_type": "api_key"}
    ]
    assert exc.value.needs_credential == {"requirement_id": "api_key", "provider": "github-reader"}
    assert repo.rows == {}  # nothing persisted
    assert enqueued == []  # nothing handed to the worker — zero tokens


async def test_a_capability_the_org_has_no_instance_of_at_all_is_also_a_miss() -> None:
    """No instance is the everyday shape for a fresh org: nothing to validate, still not runnable.
    The credential type comes from the tool's own descriptor, so the prompt still says WHAT to
    connect."""
    from oraclous_execution_engine_service.services.team_run_service import TeamRunPreflightError

    registry = _FakeRegistry(tools=[_tool(_READER_ID, "GitHub Reader")], instances=[])
    svc, repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    with pytest.raises(TeamRunPreflightError) as exc:
        await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert exc.value.needs_credential == {"requirement_id": "api_key", "provider": "github-reader"}
    assert repo.rows == {} and enqueued == []


async def test_every_unmet_binding_is_reported_and_the_first_is_the_prompt() -> None:
    """Two members, two unconfigured tools: the user is told about both (``missing``), and the
    connect prompt names the first in member order — one prompt at a time, like the console."""
    from oraclous_execution_engine_service.services.team_run_service import TeamRunPreflightError

    registry = _FakeRegistry(
        tools=[
            _tool(_READER_ID, "GitHub Reader"),
            _tool(_SEARCH_ID, "Web Search", credential_type="api_key"),
        ],
        instances=[],
    )
    svc, _repo, _enq = _service(registry)
    manifest = _team(
        [_agent("fetcher", tools=["github-reader"]), _agent("searcher", ["web-search"])]
    )
    subs = {
        "fetcher": _sub("fetcher", [_reader_cap()]),
        "searcher": _sub("searcher", [{"ref": "core/web-search@1.0.0", "binding": "web-search"}]),
    }

    with pytest.raises(TeamRunPreflightError) as exc:
        await _go(svc, manifest, subs)

    assert [m["binding"] for m in exc.value.missing] == ["github-reader", "web-search"]
    assert exc.value.needs_credential["provider"] == "github-reader"


# ── the runs that must still start ─────────────────────────────────────────────────────────────


async def test_a_ready_instance_admits_the_run() -> None:
    registry = _FakeRegistry(
        tools=[_tool(_READER_ID, "GitHub Reader")],
        instances=[_instance(_READER_ID, status="READY", mappings={"api_key": "cred-1"})],
    )
    svc, repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    row = await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert row.state == "QUEUED"
    assert enqueued == [row.id]
    assert any(c.startswith("validate:") for c in registry.calls)  # the registry's verdict, asked


async def test_one_ready_instance_among_unconfigured_siblings_is_enough() -> None:
    """#663 binds the org's configured sibling at dispatch, so a stale unconfigured copy beside a
    READY one must not block the run the harness would have bound correctly."""
    stale = _instance(_READER_ID, status="CONFIGURATION_REQUIRED")
    ready = _instance(_READER_ID, status="READY", mappings={"api_key": "cred-1"})
    registry = _FakeRegistry(tools=[_tool(_READER_ID, "GitHub Reader")], instances=[stale, ready])
    svc, _repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    row = await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert enqueued == [row.id]


async def test_a_team_declaring_no_tools_makes_no_registry_call_and_still_runs() -> None:
    """Criterion 4: a pure-reasoning team is unaffected — not even a list call."""
    registry = _FakeRegistry(tools=[_tool(_READER_ID, "GitHub Reader")], instances=[])
    svc, _repo, enqueued = _service(registry)
    manifest = _team([_agent("thinker"), _agent("writer")])

    row = await _go(svc, manifest, {})

    assert enqueued == [row.id]
    assert registry.calls == []


async def test_a_binding_carrying_its_own_credential_mappings_is_not_preflighted() -> None:
    """The harness mints and configures a fresh instance from the manifest's own
    ``credential_mappings`` (#663 mint path) — there is no org instance to check, and refusing
    would be a false negative for a manifest that is about to work."""
    registry = _FakeRegistry(tools=[_tool(_READER_ID, "GitHub Reader")], instances=[])
    svc, _repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])
    sub = _sub("fetcher", [_reader_cap({"credential_mappings": {"api_key": "cred-9"}})])

    row = await _go(svc, manifest, {"fetcher": sub})

    assert enqueued == [row.id]
    assert not any(c.startswith("validate:") for c in registry.calls)


async def test_the_preflight_runs_before_the_row_is_written() -> None:
    """Ordering, pinned: a miss discovered AFTER persisting would leave a QUEUED row the worker
    never drives — the strand #664 exists to prevent, in a new place."""
    from oraclous_execution_engine_service.services.team_run_service import TeamRunPreflightError

    registry = _FakeRegistry(tools=[_tool(_READER_ID, "GitHub Reader")], instances=[])
    svc, repo, _enq = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    with pytest.raises(TeamRunPreflightError):
        await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert repo.rows == {}


# ── fail closed, never skip ────────────────────────────────────────────────────────────────────


async def test_an_unreachable_registry_is_a_502_not_an_admitted_run() -> None:
    registry = _FakeRegistry(unreachable=True)
    svc, repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    with pytest.raises(TeamRunError) as exc:
        await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert exc.value.status_code == 502
    assert exc.value.error_type == "registry_unavailable"
    assert repo.rows == {} and enqueued == []


async def test_with_no_registry_wired_the_check_is_skipped_like_the_graph_check() -> None:
    """The unit-test posture every existing create test relies on (``registry=None``): the
    harness's own #663 gate stays the second line. The REAL request path always wires one."""
    svc, _repo, enqueued = _service(None)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    row = await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert enqueued == [row.id]
