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
  <credential_type>, "provider": <the registered tool's own slug>}`` for the FIRST miss —
  NOT the team's ``binding`` alias, which can differ (see the connect-prompt test below): the
  leak-safe pair the
  gateway already relays as CREDENTIALS_REQUIRED and the console already renders as a connect
  prompt. Nothing is persisted, nothing is enqueued, so nothing can spend.
* A team whose members declare no tools makes no registry call at all (criterion 4). A binding
  whose sub-harness carries its own ``config.credential_mappings`` is skipped — the harness mints
  and configures a fresh instance for it, so refusing would be a false negative. An unreachable
  registry — or a registry that answers a verdict with a non-2xx — is a 502
  ``registry_unavailable`` (fail closed, the #695 posture), never a skip and never a miss pinned
  on the user. With no registry wired at all the check is not run — the ``graphs`` precedent —
  and the harness gate (#663) remains the second line.
* NARROW, so it never refuses a run the harness would have bound. The pre-flight applies only to
  the MAPPING-LOAD-BEARING credential types (``api_key`` / ``connection_string`` /
  ``username_password`` — the harness's own ``_mapped_credential_types`` line, #663): a tool
  that declares no such requirement is left to the harness to mint, whether it is a keyless
  first-party connector (``graph-ingest``, ``write``), an OAuth-only tool (the broker resolves
  the user's grant per request, no mapping needed — the registry's verdict would flag the
  unmapped ``oauth_token``, so the verdict is not even asked), or an imported MCP tool carrying
  its own ``spec.credential_id`` (#698 D2). A ref the org has not registered is left to the
  harness too, whose ``_resolve_all`` fails before any model token. Known gap, accepted: an
  OAuth-only tool whose user has no grant still spends up to its first 409.
* A binding is matched to a registered tool by SLUG — the ref's name slug against the tool row's
  name slug (``core/web-research@1.0.0`` ⇔ ``Web Research``), exactly as the harness resolves.

New-symbol imports are function-local (§4.1) so collection never breaks other suites.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.registry_client import (
    RegistryClientError,
    RegistryRejected,
)
from oraclous_execution_engine_service.services.team_run_service import TeamRunError, TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_READER_ID = uuid.uuid4()
_SEARCH_ID = uuid.uuid4()
_GRAPH_ID = uuid.uuid4()
_MCP_ID = uuid.uuid4()
_OAUTH_ID = uuid.uuid4()


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


def _tool(
    capability_id: uuid.UUID,
    name: str,
    *,
    credential_type: str | None = "api_key",
    spec: dict[str, Any] | None = None,
) -> dict:
    """A ``GET /api/v1/tools`` row: the registry's own name + the descriptor's requirements.
    ``credential_type=None`` declares a keyless tool; ``spec`` overrides the whole spec."""
    if spec is None:
        spec = {
            "credential_requirements": (
                [{"type": credential_type, "required": True}] if credential_type else []
            )
        }
    return {
        "id": str(capability_id),
        "name": name,
        "kind": "tool",
        "status": "active",
        "descriptor": {"spec": spec},
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
        verdicts: dict[str, dict] | None = None,
        reject_validate: int | None = None,
    ) -> None:
        self.tools = tools or []
        self.instances = instances or []
        self.unreachable = unreachable
        # a scripted ValidationReport per instance id — overrides the row-status rule below, so a
        # row that SAYS READY can answer "not ready" the way a revoked credential does (S1)
        self.verdicts = verdicts or {}
        self.reject_validate = reject_validate  # a non-2xx from validate-execution (S2)
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
        if self.reject_validate is not None:
            raise RegistryRejected(self.reject_validate, "boom")
        if str(instance_id) in self.verdicts:
            return dict(self.verdicts[str(instance_id)])
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
        {
            "role": "fetcher",
            "binding": "github-reader",
            "credential_type": "api_key",
            "provider": "github-reader",
        }
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
            _tool(_SEARCH_ID, "Web Research", credential_type="api_key"),
        ],
        instances=[],
    )
    svc, _repo, _enq = _service(registry)
    manifest = _team(
        [_agent("fetcher", tools=["github-reader"]), _agent("searcher", ["web-research"])]
    )
    subs = {
        "fetcher": _sub("fetcher", [_reader_cap()]),
        "searcher": _sub(
            "searcher", [{"ref": "core/web-research@1.0.0", "binding": "web-research"}]
        ),
    }

    with pytest.raises(TeamRunPreflightError) as exc:
        await _go(svc, manifest, subs)

    assert [m["binding"] for m in exc.value.missing] == ["github-reader", "web-research"]
    assert exc.value.needs_credential["provider"] == "github-reader"


async def test_criterion_3_a_ready_row_whose_credential_no_longer_resolves_is_a_miss() -> None:
    """ "CONFIGURATION_REQUIRED counts, not just an absent credential row": the verdict, not the
    row. A mapped-then-revoked credential leaves the row at READY until something re-validates
    it; the registry's ``validate-execution`` resolves it through the broker and says otherwise.
    An impl that reads ``instance["status"]`` and never asks would admit this run."""
    from oraclous_execution_engine_service.services.team_run_service import TeamRunPreflightError

    stale = _instance(_READER_ID, status="READY", mappings={"api_key": "cred-revoked"})
    registry = _FakeRegistry(
        tools=[_tool(_READER_ID, "GitHub Reader")],
        instances=[stale],
        verdicts={
            stale["id"]: {
                "is_ready": False,
                "instance_id": stale["id"],
                "status": "CONFIGURATION_REQUIRED",
                "checks": {"capability": "passed", "credentials": "failed"},
                "errors": [
                    {
                        "type": "CREDENTIAL_UNRESOLVABLE",
                        "message": "the credential connected for 'api_key' could not be resolved",
                        "severity": "critical",
                        "credential_type": "api_key",
                    }
                ],
                "action_items": [],
            }
        },
    )
    svc, repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    with pytest.raises(TeamRunPreflightError) as exc:
        await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert exc.value.needs_credential == {"requirement_id": "api_key", "provider": "github-reader"}
    assert repo.rows == {} and enqueued == []


# ── the runs that must still start ─────────────────────────────────────────────────────────────


async def test_a_tool_that_needs_no_credential_is_left_to_the_harness_to_mint() -> None:
    """Every keyless first-party connector (``graph-ingest``, ``write``, ``read``…) has NO org
    instance until the harness mints ``harness:<id>:<binding>`` at dispatch. "No instance ⇒ miss"
    would refuse every compiled team."""
    registry = _FakeRegistry(tools=[_tool(_GRAPH_ID, "Graph Ingest", credential_type=None)])
    svc, _repo, enqueued = _service(registry)
    manifest = _team([_agent("writer", tools=["graph-ingest"])])
    sub = _sub("writer", [{"ref": "core/graph-ingest@1.0.0", "binding": "graph-ingest"}])

    row = await _go(svc, manifest, {"writer": sub})

    assert enqueued == [row.id]
    assert not any(c.startswith("validate:") for c in registry.calls)


async def test_an_imported_mcp_tool_carrying_its_own_key_is_left_to_the_harness() -> None:
    """#698 D2: an MCP import records the key the org admin chose (``spec.credential_id``) AND
    declares an ``api_key`` requirement; the registry falls back to that key at dispatch and the
    harness exempts it. The org never holds an instance of it — refusing would 409 the D7
    pull-request-review team at GO."""
    mcp = _tool(
        _MCP_ID,
        "github-mcp",
        spec={
            "type": "mcp",
            "credential_id": "cred-7",
            "credential_requirements": [{"type": "api_key", "required": True, "provider": "mcp"}],
        },
    )
    registry = _FakeRegistry(tools=[mcp])
    svc, _repo, enqueued = _service(registry)
    manifest = _team([_agent("reader", tools=["github-mcp"])])
    sub = _sub("reader", [{"ref": "org:mcp/github-mcp@1", "binding": "github-mcp"}])

    row = await _go(svc, manifest, {"reader": sub})

    assert enqueued == [row.id]
    assert not any(c.startswith("validate:") for c in registry.calls)


async def test_an_oauth_only_tool_is_never_refused_at_go() -> None:
    """OAuth is broker-resolved per request from the user's grant; no instance mapping is needed
    and the harness's #663 gate exempts it. The registry's verdict WOULD flag the unmapped
    ``oauth_token``, so the verdict is not asked — with no instance, and with a
    CONFIGURATION_REQUIRED one. (Known gap: a user with no grant still spends up to the first 409.)
    """
    oauth = _tool(_OAUTH_ID, "Google Drive", credential_type="oauth_token")
    stale = _instance(_OAUTH_ID, status="CONFIGURATION_REQUIRED", required=["oauth_token"])
    manifest = _team([_agent("reader", tools=["google-drive"])])
    sub = _sub("reader", [{"ref": "core/google-drive@1.0.0", "binding": "google-drive"}])

    for instances in ([], [stale]):
        registry = _FakeRegistry(tools=[oauth], instances=instances)
        svc, _repo, enqueued = _service(registry)
        row = await _go(svc, manifest, {"reader": sub})
        assert enqueued == [row.id], instances
        assert not any(c.startswith("validate:") for c in registry.calls), instances


async def test_a_ref_the_org_has_not_registered_is_left_to_the_harness() -> None:
    """The harness's ``_resolve_all`` fails a ref no registry row matches, before any model
    token — so the pre-flight does not duplicate that refusal (and never guesses a credential
    type for a tool it cannot see)."""
    registry = _FakeRegistry(tools=[], instances=[])
    svc, _repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    row = await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert enqueued == [row.id]
    assert not any(c.startswith("validate:") for c in registry.calls)


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


async def test_a_verdict_the_registry_refuses_to_give_is_a_502_not_a_miss_and_not_a_run() -> None:
    """A 5xx (or a 404 for an instance that vanished between list and validate) is inconclusive:
    never admitted, and never reported to the user as THEIR missing credential."""
    registry = _FakeRegistry(
        tools=[_tool(_READER_ID, "GitHub Reader")],
        instances=[_instance(_READER_ID, status="READY", mappings={"api_key": "c"})],
        reject_validate=500,
    )
    svc, repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    with pytest.raises(TeamRunError) as exc:
        await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert exc.value.status_code == 502
    assert exc.value.error_type == "registry_unavailable"
    assert repo.rows == {} and enqueued == []


# --- review round 1: a mixed-credential descriptor, a mislabelled connect target, an ---------
# --- inconclusive verdict admitting the run --------------------------------------------------


async def test_a_mixed_credential_descriptor_with_the_load_bearing_type_mapped_is_admitted() -> (
    None
):
    """A tool declaring BOTH ``api_key`` (load-bearing) and ``oauth_token`` (broker-resolved,
    never mapped) must be admitted once the org's instance maps the load-bearing type — the
    harness binds it (``_mapped_credential_types`` only ever asks for the load-bearing set). The
    registry's verdict still reports the unmapped ``oauth_token`` as a CREDENTIAL_NOT_CONFIGURED
    error; that error must not count against a binding whose only NEEDED type is satisfied."""
    mixed = _tool(
        _READER_ID,
        "GitHub Reader",
        spec={
            "credential_requirements": [
                {"type": "api_key", "required": True},
                {"type": "oauth_token", "required": True},
            ]
        },
    )
    mapped = _instance(
        _READER_ID,
        status="CONFIGURATION_REQUIRED",
        mappings={"api_key": "c"},
        required=["api_key", "oauth_token"],
    )
    registry = _FakeRegistry(
        tools=[mixed],
        instances=[mapped],
        verdicts={
            mapped["id"]: {
                "is_ready": False,
                "instance_id": mapped["id"],
                "status": "CONFIGURATION_REQUIRED",
                "checks": {"capability": "passed", "credentials": "failed"},
                "errors": [
                    {
                        "type": "CREDENTIAL_NOT_CONFIGURED",
                        "message": "required credential 'oauth_token' is not configured",
                        "severity": "critical",
                        "credential_type": "oauth_token",
                    }
                ],
                "action_items": [],
            }
        },
    )
    svc, _repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    row = await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert enqueued == [row.id]


async def test_the_connect_prompt_names_the_tools_own_slug_not_the_binding_alias() -> None:
    """A member's ``tools[]``/sub-harness ``binding`` is a free alias the team's own author chose
    (it can differ from the registered tool's name); ``provider`` must name the TOOL the credential
    is actually for, so the console connects the right thing — not whatever alias this team used."""
    registry = _FakeRegistry(tools=[_tool(_READER_ID, "GitHub Reader")], instances=[])
    svc, _repo, _enq = _service(registry)
    manifest = _team([_agent("fetcher", tools=["reader"])])  # a DIFFERENT alias than the tool name
    sub = _sub("fetcher", [{"ref": "core/github-reader@1.0.0", "binding": "reader"}])

    from oraclous_execution_engine_service.services.team_run_service import TeamRunPreflightError

    with pytest.raises(TeamRunPreflightError) as exc:
        await _go(svc, manifest, {"fetcher": sub})

    assert exc.value.needs_credential == {"requirement_id": "api_key", "provider": "github-reader"}


async def test_a_broker_unreachable_verdict_is_inconclusive_not_a_ready_admission() -> None:
    """The registry answers a WARNING (not an error) when it cannot reach the credential broker —
    it genuinely does not know. Reading only ``is_ready`` (true when nothing FAILED) admits a run
    the harness may still 409 on; this must fail closed the same as any other inconclusive read."""
    row = _instance(_READER_ID, status="READY", mappings={"api_key": "c"})
    registry = _FakeRegistry(
        tools=[_tool(_READER_ID, "GitHub Reader")],
        instances=[row],
        verdicts={
            row["id"]: {
                "is_ready": True,
                "instance_id": row["id"],
                "status": "READY",
                "checks": {"capability": "passed", "credentials": "warning"},
                "errors": [],
                "action_items": [],
            }
        },
    )
    svc, repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    with pytest.raises(TeamRunError) as exc:
        await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert exc.value.status_code == 502
    assert exc.value.error_type == "registry_unavailable"
    assert repo.rows == {} and enqueued == []


async def test_a_verdict_with_no_readable_shape_is_a_502_not_an_admission() -> None:
    """A verdict body that is not JSON, or carries no ``is_ready`` key, is unreadable — the
    inconclusive direction is the 502, never a silent admission and never an unhandled crash."""
    row = _instance(_READER_ID, status="READY", mappings={"api_key": "c"})
    registry = _FakeRegistry(
        tools=[_tool(_READER_ID, "GitHub Reader")], instances=[row], verdicts={row["id"]: {}}
    )
    svc, repo, enqueued = _service(registry)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    with pytest.raises(TeamRunError) as exc:
        await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert exc.value.status_code == 502
    assert repo.rows == {} and enqueued == []


async def test_with_no_registry_wired_the_check_is_skipped_like_the_graph_check() -> None:
    """The unit-test posture every existing create test relies on (``registry=None``): the
    harness's own #663 gate stays the second line. The REAL request path always wires one."""
    svc, _repo, enqueued = _service(None)
    manifest = _team([_agent("fetcher", tools=["github-reader"])])

    row = await _go(svc, manifest, {"fetcher": _sub("fetcher", [_reader_cap()])})

    assert enqueued == [row.id]
