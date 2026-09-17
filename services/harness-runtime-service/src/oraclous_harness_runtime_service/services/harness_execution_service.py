"""Harness execution spine (services layer).

Turns an OHM + an input into a completed run:
  source the manifest (inline YAML/object, or a ``manifest_ref`` → a registered kind=harness
  descriptor) → verify signatures against the trust store → compute the content hash → validate the
  schema → **atomically resolve every capability** against the registry (all-or-nothing) →
  materialise an instance per capability (+ its credential mappings) → expose the union of all
  capabilities as the agent's tools → run the tool-use loop, dispatching each call to the registry's
  real execute → persist the run row → emit a provenance event per step + a closure event.

OHM errors (parse/schema/version/reference/signature) propagate to the route (422); a registry
*setup* failure is a ``HarnessExecutionError`` (502); per-tool failures are fed back into the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

import yaml
from oraclous_governance import Principal
from oraclous_ohm.canonical import content_hash
from oraclous_ohm.errors import OHMParseError, OHMReferenceError
from oraclous_ohm.parse import load_ohm
from oraclous_ohm.references import resolve_capabilities
from oraclous_ohm.signatures import TrustStore, verify_signatures
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord
from oraclous_telemetry import Severity, alert

from oraclous_harness_runtime_service.domain.llm.base import LLMClient, ToolSpec
from oraclous_harness_runtime_service.domain.llm.egress import (
    EgressBlockedError,
    validate_outbound_url,
)
from oraclous_harness_runtime_service.domain.llm.factory import (
    LLMConfigError,
    build_fake_client,
    build_live_client,
)
from oraclous_harness_runtime_service.domain.loop.progress import LoopProgress
from oraclous_harness_runtime_service.domain.loop.tool_use import (
    _REPEATED_FAILURE_STATUS,
    LoopCheckpoint,
    LoopResult,
    LoopStep,
    run_tool_use_loop,
)
from oraclous_harness_runtime_service.domain.policy import (
    build_envelope,
    enforce_load_policy,
    resolve_policy_set,
)
from oraclous_harness_runtime_service.domain.tool_schemas import (
    OperationOverrideRefused,
    ToolDispatchRefused,
    dispatch_payload,
    tool_specs_for,
)
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind
from oraclous_harness_runtime_service.models.execution import HarnessExecution
from oraclous_harness_runtime_service.repositories.assignment_repository import AssignmentRepository
from oraclous_harness_runtime_service.repositories.checkpoint_repository import CheckpointRepository
from oraclous_harness_runtime_service.repositories.execution_lease_repository import (
    DuplicateExecutionId,
    ExecutionLeaseRepository,
)
from oraclous_harness_runtime_service.repositories.execution_repository import ExecutionRepository
from oraclous_harness_runtime_service.services.broker_client import BrokerClient, BrokerError
from oraclous_harness_runtime_service.services.memory_client import MemoryReader, MemoryWriter
from oraclous_harness_runtime_service.services.registry_client import (
    RegistryClient,
    RegistryError,
    capability_slug,
)

logger = logging.getLogger(__name__)

_RESERVED_CONFIG_KEYS = ("credential_mappings", "capability_id")

# #743 (Contract #735 §CITE): the first-party retrieval capabilities whose results the loop may
# believe when they carry the served-citation-ids reserved key. Matched on the REGISTRY row's own
# name, never on the manifest's binding alias — a manifest may bind core/knowledge-retriever as
# "retriever" or "Read", and equally may name an imported MCP binding "knowledge-retriever". #746
# adds the live web / MCP rows of §CITE's minting table to this set.
_CITATION_MINTING_CAPABILITIES = frozenset(
    {"knowledge-retriever", "federated-search", "find-similar"}
)

# #781 (Contract #735 §CITE, security): the capabilities whose results the loop may believe when
# they carry #580's `data_absent` key. Kept separate from — and narrower than — the citation set
# above, on purpose. `data_absent` is emitted in exactly ONE place in the platform, the
# knowledge-retriever connector; neither federated-search nor find-similar sets it. #746 extends the
# MINTING set to live web reads and imported MCP tools, whose results are whatever a remote server
# returned; sharing one set would silently extend absence-trust to those rows too, which is the
# echo-shaped surface #781 exists to close. Two sets, derived together right below, so #746 sees
# both and states which one it means.
_DATA_ABSENCE_CAPABILITIES = frozenset({"knowledge-retriever"})

# #961: the capabilities the site-restriction gate fires on. TWO, not one: the platform ships its
# web search under `Web Research`'s `search` operation and under the standard `WebSearch` tool, and
# they share one code path. A set naming only the first would leave every team built from the
# standard toolset unenforced, with nothing to say why.
#
# A THIRD set rather than a widening of the two above, for #781's reason: each reserved behaviour
# trusts the capabilities that actually carry it. The web search neither mints citations nor flags
# graph data-absence, and a shape that folded these together would silently grant it both.
_WEB_SEARCH_CAPABILITIES = frozenset({"web-research", "websearch"})

# #968: and the predicate that pairs with it, which is deliberately NOT the one above.
#
# The two sets guard risks that point in OPPOSITE directions, and the fix for #968 is to stop
# pretending otherwise. For citations, believing a row that should not be believed mints forged
# provenance, so the dangerous direction is too WIDE and a positive `INTERNAL` allow-list is right.
# For the site restriction, a row that is NOT believed is simply not enforced — so the dangerous
# direction is too NARROW, and that is exactly what shipped: both search tools are registered
# `TYPE = "API"` (builtin.py), the `INTERNAL` allow-list matched neither, and a person's list of
# websites was ignored on every real run while every test stayed green.
#
# What #780 actually established is that an MCP-imported row must never be trusted on a name it
# borrowed, and `spec.type == "mcp"` is the reliable marker of one (the registry stamps it at
# import and gates such rows `pending_approval`). Excluding that keeps #780's property intact
# without coupling this gate to which of the platform's several first-party types a particular
# plugin happens to declare — the coupling that broke it.
_MCP_SPEC_TYPE = "mcp"

# #780 item 1 (security): the second predicate, alongside the row's name slug. A registry row's
# `name` is a DISPLAY string — for an imported MCP tool it is `<admin label>-<server tool name>`,
# both halves chosen outside the platform, so an admin importing a server labelled `knowledge` with
# a tool named `retriever` stores exactly the first-party retriever's slug. That collision loses the
# resolution today only because `list_by_kind` orders by `created_at` and the seeded row is older —
# incidental, never a stated invariant, and #746 removes the property it rests on. MCP rows carry
# `spec.type == "mcp"`, so requiring the first-party type closes it outright, and (unlike keying on
# the descriptor id) puts no registry UUIDs in the runtime.
_FIRST_PARTY_SPEC_TYPE = "INTERNAL"


class TrustedBindings(NamedTuple):
    """The binding aliases this manifest may be believed on, per reserved result key.

    One value rather than two loose frozensets so a caller cannot pass them in the wrong order, and
    so the pair travels together — the #781 decision is that these sets differ, and a shape that
    hides the difference is the shape that loses it.
    """

    citation: frozenset[str]  # #743 §CITE: may mint `served_citation_ids`
    data_absence: frozenset[str]  # #580/#781: may flag `data_absent`
    web_search: frozenset[str]  # #961: bound by a run's site restriction; may flag an empty one


#: #1111: the shape a tool execution's curated ``error_type`` may take (the registry's connectors
#: spell them ``PROVIDER_RATE_LIMITED``, ``INVALID_INPUT``, ...). Anything else is not carried.
_EXECUTION_ERROR_TYPE = re.compile(r"^[A-Z0-9_]{1,64}$")


class _ProviderError(NamedTuple):
    """How one curated provider token is classified, plus this service's own words for it.

    ``effect_unknown`` (#1111 review round 1, B1) says whether the failing call MAY ALREADY have
    taken effect at the provider — see ``RegistryError``. It is separate from ``transient``: a
    refusal is transient AND provably inert, while a lost answer is transient and ambiguous.
    """

    transient: bool
    effect_unknown: bool
    meaning: str


#: #1111 decision 2: the curated provider tokens the loop acts on, with whether a retry may clear
#: them and this service's own words for each. For these the registry's ``error_message`` is not
#: relayed — the token says everything a caller needs, and nothing upstream-authored crosses.
_PROVIDER_ERROR_TYPES: dict[str, _ProviderError] = {
    # The provider refused the call outright, before doing any of the work it asks for.
    "PROVIDER_RATE_LIMITED": _ProviderError(
        True, False, "the tool's provider is rate-limiting this organisation"
    ),
    # #1111 review round 1, M1. The connector's own outbound call to the third party failed in
    # transport — ``search_providers`` raises it from any ``httpx.HTTPError``. Transient, because a
    # network failure reaching a provider is the most retry-worthy thing in this table and the
    # alternative (no classification at all) drops it back onto the raw-prose path this issue
    # exists to close. Ambiguous, because the token is emitted from BOTH halves of the exchange:
    # a connection that never opened carries it, and so does a read timeout after the provider
    # already acted. So a retrieval retries it and an operation that may write does not.
    "PROVIDER_UNREACHABLE": _ProviderError(True, True, "the tool's provider could not be reached"),
    "PROVIDER_QUOTA_EXHAUSTED": _ProviderError(
        False, False, "the tool's credential has no remaining quota"
    ),
    "PROVIDER_AUTH_FAILED": _ProviderError(
        False, False, "the tool's credential was rejected by its provider"
    ),
}


def _tool_execution_error(execution: dict[str, Any]) -> RegistryError:
    """The ``RegistryError`` for a tool execution that completed with a non-SUCCESS status.

    Carries the registry's curated ``error_type`` as ``error_code`` and marks it ``transient`` per
    ``_PROVIDER_ERROR_TYPES``. A failure without a recognised provider token keeps today's message,
    which the model reads to adapt its next call.
    """
    raw_type = execution.get("error_type")
    error_type = (
        raw_type if isinstance(raw_type, str) and _EXECUTION_ERROR_TYPE.match(raw_type) else None
    )
    provider = _PROVIDER_ERROR_TYPES.get(error_type) if error_type is not None else None
    if provider is not None:
        return RegistryError(
            f"tool execution failed ({error_type}): {provider.meaning}",
            error_code=error_type,
            transient=provider.transient,
            effect_unknown=provider.effect_unknown,
        )
    detail = execution.get("error_message") or execution.get("status")
    return RegistryError(f"tool execution failed: {detail}", error_code=error_type)


def _trusted_bindings(
    manifest,  # noqa: ANN001
    resolved: dict[str, dict[str, Any]],
) -> TrustedBindings:
    """Translate the trusted CAPABILITIES into the binding aliases THIS manifest gave them.

    The loop cannot key trust on a ``ToolSpec.binding`` — that alias is the manifest author's free
    choice, so a manifest could name an imported MCP binding ``knowledge-retriever`` and be
    believed. ``resolved`` is the registry's answer, and each row must clear BOTH predicates:

    * the row's own ``name``, slugified, is one of the capabilities that emit the key (#743);
    * the row is first-party — ``spec.type == "INTERNAL"`` (#780 item 1), which an imported MCP row
      (``spec.type == "mcp"``) can never satisfy however its admin-chosen name happens to slug.

    A binding that clears neither is simply absent from the set, and the loop strips its reserved
    key without believing it. Fail-closed: a row with no readable descriptor is untrusted.
    """
    citation: set[str] = set()
    data_absence: set[str] = set()
    web_search: set[str] = set()
    for cap in manifest.capabilities:
        row = resolved.get(cap.binding) or {}
        descriptor = row.get("descriptor") or {}
        spec = descriptor.get("spec") or {}
        if not isinstance(spec, dict):
            continue  # fail-closed: a row with no readable descriptor is trusted for nothing
        slug = capability_slug(str(row.get("name") or ""))
        # #968: the web-search set uses the not-an-import predicate; see its note above. It is
        # evaluated BEFORE the `INTERNAL` gate below, because that gate is what excluded it.
        if slug in _WEB_SEARCH_CAPABILITIES and spec.get("type") != _MCP_SPEC_TYPE:
            web_search.add(cap.binding)
        if spec.get("type") != _FIRST_PARTY_SPEC_TYPE:
            continue
        if slug in _CITATION_MINTING_CAPABILITIES:
            citation.add(cap.binding)
        if slug in _DATA_ABSENCE_CAPABILITIES:
            data_absence.add(cap.binding)
    return TrustedBindings(
        citation=frozenset(citation),
        data_absence=frozenset(data_absence),
        web_search=frozenset(web_search),
    )


# #663 — the credential types whose broker resolution READS an instance's credential_mappings.
# oauth_token is deliberately absent: the broker resolves it per request from (org, user, provider,
# scopes) and ignores mappings, so an OAuth-only capability needs no configured instance.
_MAPPED_CREDENTIAL_TYPES = frozenset({"api_key", "connection_string", "username_password"})


def _mapped_credential_types(descriptor: dict[str, Any]) -> list[str]:
    """The descriptor's REQUIRED credential types that an instance must have mapped to dispatch
    (mirrors the registry's ``required_credential_types``: ``required`` defaults to true; distinct,
    order-stable), restricted to the mapping-load-bearing types above."""
    spec = descriptor.get("spec") or {}
    # #698 D2 — an MCP import carries the key the org admin already chose for that server, and the
    # registry resolves it at dispatch when the instance maps nothing. The #663 gate below exists
    # because a fresh mint could never be configured; this one arrives configured, so demanding a
    # separately configured instance would fail a run that is about to work. The requirement itself
    # is not dropped: an unresolvable id is still a typed needs_credential at dispatch.
    if spec.get("type") == "mcp" and spec.get("credential_id"):
        return []
    requirements = spec.get("credential_requirements") or []
    out: list[str] = []
    for req in requirements:
        if not isinstance(req, dict) or not req.get("required", True):
            continue
        cred_type = req.get("type")
        if cred_type in _MAPPED_CREDENTIAL_TYPES and cred_type not in out:
            out.append(cred_type)
    return out


def _per_run_configuration(
    *,
    workspace_root: str | None,
    graph_id: str | None,
    precedence_order: list[str] | None,
    graph_authoritative: bool,
    producer: dict[str, Any] | None,
) -> dict[str, Any]:
    """The instance-configuration keys that belong to THIS RUN, not to the instance.

    Built in one place (#1130) because the two callers below — the fresh mint and the reuse of a
    deterministically-named instance — must bind exactly the same set: a seeded app's sub-harness
    id is stable across runs (``uuid.uuid5(app_id, role)``), so the second and every later run of
    the same app takes the reuse path, and any key bound only on the mint path would stay frozen
    at whatever the FIRST run wrote.

    - ``working_dir`` (#518): the team run's trusted working tree, so file tools operate in place
      (org-confined by the registry sandbox guard, #517).
    - ``graph_id`` (#524): the run's graph, so graph tools target it — the model never invents a
      UUID (org-scoped at create + by KGS RLS).
    - ``precedence`` (#538, Hierarchy of Truth): the team's precedence, so the retriever ranks each
      member's in-loop read canonical-first (#536). Bound on every instance; only the retriever
      connector reads it. Empty order → left unbound.
    - the producer fields (#728): WHO is writing, so an artifact records the member and run that
      produced it. Instance configuration is the trusted channel ``graph_id`` already uses, so the
      model can neither supply nor forge its own identity. Only the graph-ingest connector reads
      them; every other instance carries them inertly.
    """
    per_run: dict[str, Any] = {}
    if workspace_root is not None:
        per_run["working_dir"] = workspace_root
    if graph_id is not None:
        per_run["graph_id"] = graph_id
    if precedence_order:
        per_run["precedence"] = {
            "order": precedence_order,
            "graph_authoritative": graph_authoritative,
        }
    if producer:
        per_run.update(producer)
    return per_run


# Run states that count as a COMPLETED run for the post-run memory hook (#332 / ADR-027 §5) — an
# ESCALATED pause (HITL / human assignment) is not a completed run, so no memory is written for it.
# #587: a PARTIAL (degrade) run FINISHED with best-effort output (checkpoint=None), so it grounds
# memory like SUCCEEDED/FAILED — the learning signal from a degraded run is not dropped.
_MEMORY_TERMINAL_STATUSES = (
    HarnessStatus.SUCCEEDED.value,
    HarnessStatus.FAILED.value,
    HarnessStatus.PARTIAL.value,
)


class HarnessExecutionError(Exception):
    """A harness could not be set up to run (a dependency/registry failure). Maps to HTTP 502."""


class ResumeError(Exception):
    """A mid-loop HITL run could not be resumed (missing/wrong-state). Carries the HTTP status."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class CancelError(Exception):
    """``cancel()`` found nothing it may act on for this ``(execution_id, organisation_id)`` pair —
    an unknown id, or one that belongs to another organisation (#1072 design ruling: identical in
    both cases, so a wrong-org caller learns nothing about whether the id exists at all). Carries
    the HTTP status (404 for both)."""

    def __init__(self, message: str, status_code: int = 404) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class CancelPending:
    """``cancel()``'s answer when the cancel flag was set but no terminal row landed before
    ``cancel_wait_seconds`` elapsed (#1072 design ruling) — the route maps this to 202."""

    execution_id: uuid.UUID
    status: str = "CANCEL_REQUESTED"


def _serialize_steps(
    steps: list[LoopStep], base: int = 0, protocol_shape: str | None = None
) -> list[dict[str, Any]]:
    """LoopSteps → the JSONB step-trace shape, re-indexed from ``base`` (resume appends a tail)."""
    return [
        {
            "index": base + i,
            "kind": s.kind.value,
            "name": s.name,
            "status": s.status,
            "detail": s.detail,
            # #641: the durable claim→receipt link. Persisted on every step (None for LLM/gate
            # steps) so the engine can resolve a member's driving_signals against its own trace.
            "tool_call_id": s.tool_call_id,
            # #828 item 2: the step's wall-clock bounds (None for a synthetic bookkeeping step). ISO
            # strings, not raw datetimes — this dict is persisted into a JSONB column whose
            # serializer has no datetime support (a raw datetime 500s every write, caught live on
            # PR #859's deployed-stack e2e). StepOut parses the string back into a datetime same as
            # any other JSON-sourced timestamp on a read.
            "started_at": s.started_at.isoformat() if s.started_at is not None else None,
            "ended_at": s.ended_at.isoformat() if s.ended_at is not None else None,
            # #907: which LLM client ran this segment (the loop's own protocol_shape) — stamped on
            # every step, same for all of them, because the client never changes mid-run.
            "protocol_shape": protocol_shape,
        }
        for i, s in enumerate(steps)
    ]


def _primary_model_binding(manifest) -> str | None:  # noqa: ANN001
    """The OHM primary model's binding (the full ``<provider>/<model-id>`` string, e.g.
    ``openrouter/openai/gpt-4o-mini``) — recorded per execution so spend can be priced by model.
    ``None`` when the manifest itself declares no model. #907: this reads the MANIFEST, not which
    LLM client actually ran — a simulated run (``HARNESS_LLM_MODE=fake``) of a model-bound manifest
    still persists that real model string here; see ``HarnessExecutionOut.simulated`` for whether
    the client that ran was the scripted stand-in."""
    model = manifest.primary_model()
    return model.binding if model is not None else None


def _project_resolved_schema(
    descriptor: dict[str, Any], resolved_schema: dict[str, Any] | None
) -> dict[str, Any]:
    """#900 (ADR-053 decision 1): thread a capability's per-run ``resolved_schema`` onto its
    descriptor's operation as the existing ``parameters_schema`` override key
    (``tool_schemas.py``'s ``_parameters_for`` priority-1 branch already returns it unchanged,
    outright) — plus the explicit ``parameters_schema_strict`` marker (#898): a resolved_schema
    with no marker would stay NON-strict regardless of how closed it looks, which is exactly the
    fail-open #898 closed. Applies ONLY when the descriptor declares EXACTLY ONE operation on
    ``spec.capabilities`` — ambiguous with two or more (CLAUDE.md fail-closed default: ignore,
    never guess). Returns a FRESH dict; never mutates the caller's descriptor, which may be a
    shared/cached object.
    """
    if resolved_schema is None:
        return descriptor
    capabilities = (descriptor.get("spec") or {}).get("capabilities") or []
    if len(capabilities) != 1:
        return descriptor
    projected = copy.deepcopy(descriptor)
    op = projected["spec"]["capabilities"][0]
    op["parameters_schema"] = resolved_schema
    op["parameters_schema_strict"] = True
    return projected


def _is_hitl_pause(result: LoopResult) -> bool:
    return (
        result.status is HarnessStatus.ESCALATED
        and result.error_type == "hitl_required"
        and result.checkpoint is not None
    )


def _cursor(
    checkpoint: LoopCheckpoint,
    member_max_tokens: int | None = None,
    member_max_tool_calls: int | None = None,
    member_on_exhaustion: str | None = None,
    member_requires_valid_json: bool = False,
    member_answer_from_tool: str | None = None,
    json_repair_used: bool = False,
    json_repair_grant: int = 0,
    output_repair_used: bool = False,
    output_repair_grant: int = 0,
    required_sites: tuple[str, ...] = (),
    declared_output_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "iteration": checkpoint.iteration,
        "tool_calls_made": checkpoint.tool_calls_made,
        "tokens_used": checkpoint.tokens_used,
        # #576: persist the member's per-member cap so resume re-applies it (None when unset).
        "member_max_tokens": member_max_tokens,
        "member_max_tool_calls": member_max_tool_calls,
        # #587: persist on_exhaustion so a resumed (escalated) run re-applies the choice.
        "member_on_exhaustion": member_on_exhaustion,
        # #853: persist the structured-output declaration so a resumed run keeps checking, and the
        # repair state with it. A grant already earned must come back, or the corrected document
        # meets a budget gate on resume; a spent one-shot must come back too, or every pause renews
        # it. The checkpoint carries both — this only writes them where they survive the pause.
        "member_requires_valid_json": member_requires_valid_json,
        # #900: persist the declared answer tool so a resumed run still ends on it.
        "member_answer_from_tool": member_answer_from_tool,
        "json_repair_used": json_repair_used,
        "json_repair_grant": json_repair_grant,
        # #1111: the final-answer correction's one-shot state rides the pause the same way.
        "output_repair_used": output_repair_used,
        "output_repair_grant": output_repair_grant,
        # #1111 decision 4: the recovery retries already spent, so a resumed run's `attempts` keeps
        # counting instead of starting over at 1.
        # (getattr: a checkpoint-shaped double built before #1111 carries no such attribute.)
        "recovery_retries": getattr(checkpoint, "recovery_retries", 0),
        # #961: the run's restriction survives a HITL pause. Without it a paused run comes back
        # unrestricted, which is the same silent drop the whole issue exists to close — and it would
        # be reachable by any member whose search sits behind a human gate.
        "required_sites": list(required_sites),
        # #993: the run's declared output keys survive a HITL pause the same way — a resumed run
        # that came back undeclared would ship its answer unguaranteed, exactly the drop this issue
        # exists to close.
        "declared_output_keys": list(declared_output_keys),
    }


def _manifest_graph_context(manifest) -> str | None:  # noqa: ANN001
    """The run's graph context (#332 / ADR-027 §5): when the manifest binds EXACTLY ONE distinct
    ``config.graph_id`` across its capabilities, that graph is where the run's memories land;
    zero or several → None (the KGS falls back to the org-default memory graph)."""
    try:
        graph_ids = {
            str(cap.config["graph_id"])
            for cap in manifest.capabilities
            if isinstance(cap.config, dict) and cap.config.get("graph_id")
        }
    except Exception:  # noqa: BLE001 — fail-soft: the hook never hurts a run
        return None
    return graph_ids.pop() if len(graph_ids) == 1 else None


def _tool_step_names(steps: list[LoopStep]) -> list[str]:
    """The TOOL-step names for the memory hook. Fail-soft: a future shape change must never let
    this raise into the run path (the hook is best-effort), so any error yields no names."""
    try:
        return [s.name for s in steps if s.kind is StepKind.TOOL and s.name]
    except Exception:  # noqa: BLE001 — fail-soft: the hook never hurts a run
        return []


#: A TOOL step that did not succeed. ``error`` is a call that ran and failed; ``repeated_failure``
#: is #946's refusal — a call that was never dispatched because the identical call had already
#: failed identically twice.
_TOOL_FAILURE_STATUSES = frozenset({"error", _REPEATED_FAILURE_STATUS})


_PROVENANCE_ACTION = {
    StepKind.LLM: "llm.complete",
    StepKind.TOOL: "capability.invoke",
    StepKind.GATE: "governance.gate",
}


def provenance_action_for(kind: StepKind, status: str | None) -> str:
    """The provenance verb for one step.

    A TOOL step normally means a capability was invoked. A #946 refusal does NOT (security audit,
    finding 5): the call never left the harness, so recording it under the invocation verb makes
    provenance assert something that did not happen, and a consumer counting invocations cannot
    tell the two apart. §3.7's forward direction is unaffected — every real dispatch still emits.

    It matters more for a refusal than for the two pre-existing branches that share the shape (a
    ceiling denial, an unknown tool). Those are rare; a refusal is model-triggered, repeatable, and
    deliberately not charged to the tool-call budget, so the records are free to mint — this issue's
    own live proof shows eight refusals against two real calls. The wider question of what a
    provenance record should mean for the other two branches is filed separately.
    """
    if kind is StepKind.TOOL and status == _REPEATED_FAILURE_STATUS:
        return "capability.refused"
    return _PROVENANCE_ACTION.get(kind, "capability.invoke")


def _tool_step_errors(steps: list[LoopStep]) -> list[str]:
    """The names of TOOL steps that did NOT succeed (#554) — fed to the consciousness classifier so
    a recurring in-run failure (the same tool failing twice) surfaces as ``repetitive_failures``.
    Fail-soft: a future shape change must never raise into the run path (best-effort).

    #946 refusals count (security audit, finding 6). Filtering strictly on ``error`` made every
    refused repeat invisible to the one classifier whose whole purpose is spotting the same tool
    failing again and again — so from the moment the #946 bound engaged, the signal it was built to
    raise stopped arriving. A refusal is that failure recurring; it is the strongest case the
    classifier has.
    """
    try:
        return [
            s.name
            for s in steps
            if s.kind is StepKind.TOOL and s.status in _TOOL_FAILURE_STATUSES and s.name
        ]
    except Exception:  # noqa: BLE001 — fail-soft: the hook never hurts a run
        return []


class HarnessExecutionService:
    def __init__(
        self,
        *,
        registry: RegistryClient,
        broker: BrokerClient,
        executions: ExecutionRepository,
        assignments: AssignmentRepository,
        checkpoints: CheckpointRepository,
        provenance: ProvenanceCollector,
        trust: TrustStore,
        require_signature: bool,
        force_policy_set: str | None,
        llm_mode: str,
        llm_base_urls: dict[str, str],
        llm_timeout: float,
        llm_allow_private: bool,
        max_iterations: int,
        max_tokens_per_member_ceiling: int | None = None,
        max_tool_calls_per_member_ceiling: int | None = None,
        memory: MemoryWriter | None = None,
        memory_reader: MemoryReader | None = None,
        # #1072: the cross-replica cancel lease (design doc). None (the default) is the pre-#1072
        # shape — no lease, no watcher, no pre-loop duplicate-id rejection; a caller that has not
        # been updated for #1072 (deployment DI always wires a real repository) is unaffected.
        leases: ExecutionLeaseRepository | None = None,
        cancel_poll_seconds: float = 1.0,
        cancel_wait_seconds: float = 10.0,
    ) -> None:
        self._registry = registry
        self._broker = broker
        self._executions = executions
        self._assignments = assignments
        self._checkpoints = checkpoints
        self._provenance = provenance
        self._trust = trust
        self._require_signature = require_signature
        self._force_policy_set = force_policy_set
        self._llm_mode = llm_mode
        self._llm_base_urls = llm_base_urls
        self._llm_timeout = llm_timeout
        self._llm_allow_private = llm_allow_private
        self._max_iterations = max_iterations
        # #576: OPTIONAL deployment ceiling on a per-member cap (default None → OFF; the user owns
        # the budget). A configurable safety backstop, NOT the old unraisable policy tier.
        self._max_tokens_per_member_ceiling = max_tokens_per_member_ceiling
        self._max_tool_calls_per_member_ceiling = max_tool_calls_per_member_ceiling
        # The post-run memory hook (#332 / ADR-027 §5). None when HARNESS_MEMORY_WRITES is off
        # (the code default) — flag off means ZERO memory calls, not a no-op writer.
        self._memory = memory
        # The team-scope blackboard READ (#513). None → no in-loop team-memory injection (a
        # single-agent run, or memory off). Fail-soft, so a present reader never risks the run.
        self._memory_reader = memory_reader
        # #1072: the cross-replica cancel lease + its poll/wait tuning (None → the pre-#1072 shape).
        self._leases = leases
        self._cancel_poll_seconds = cancel_poll_seconds
        self._cancel_wait_seconds = cancel_wait_seconds

    async def execute(
        self,
        *,
        manifest_inline: str | dict[str, Any] | None,
        manifest_ref: str | None,
        user_input: str,
        principal: Principal,
        capability_ceiling: list[str] | None = None,
        parent_execution_id: uuid.UUID | None = None,
        trace_id: uuid.UUID | None = None,
        workspace_root: str | None = None,
        graph_id: str | None = None,
        team_id: str | None = None,
        precedence_order: list[str] | None = None,
        graph_authoritative: bool = False,
        max_tokens: int | None = None,
        max_tool_calls: int | None = None,
        on_exhaustion: Literal["escalate", "degrade"] | None = None,
        requires_valid_json: bool = False,
        answer_from_tool: str | None = None,
        required_sites: list[str] | None = None,
        declared_output_keys: list[str] | None = None,
        producer: dict[str, Any] | None = None,
        prior_fetched_urls: Collection[str] | None = None,
        person_supplied_text: str | None = None,
        execution_id: uuid.UUID | None = None,
    ) -> HarnessExecution:
        # Fail-closed tenancy (ADR-006/T1-M1): org is the principal's ONLY, never the manifest's.
        if principal.organisation_id is None:
            raise HarnessExecutionError("authenticated principal has no organisation scope")
        org_id = principal.organisation_id
        # #975 (A8): the standalone default — a caller that supplies no `person_supplied_text` gets
        # the run's own `user_input` as the seed text mined for registry URLs, so a single-agent run
        # with no engine wiring still gets the property team members get from the engine's own
        # `_render_answers` composition.
        effective_person_supplied_text = (
            person_supplied_text if person_supplied_text is not None else user_input
        )

        # Source + harden the manifest (load-time, atomic; OHMError → 422). The policy set (from
        # governance.policy_set_ref) drives coded enforcement: signature requirement, capability
        # allocation + BYOM limits at load, and the runtime budget/HITL/redaction envelope.
        document = await self._source_document(manifest_inline, manifest_ref)
        manifest = load_ohm(document)
        # A deployment-forced policy set overrides the author's choice (governance floor, M1).
        policy = resolve_policy_set(self._force_policy_set or manifest.governance.policy_set_ref)
        verify_signatures(
            document, self._trust, require=self._require_signature or policy.require_signature
        )
        enforce_load_policy(manifest, policy)
        chash = content_hash(document)

        # Actor dispatch: a human entrypoint actor halts the run as a task-board assignment
        # (R4 escalation; durable resume is R5). An agent actor (or no actors) runs the loop below.
        actor = manifest.entrypoint_actor()
        if actor is not None and actor.kind == "human":
            return await self._dispatch_human(manifest, actor, org_id, principal, user_input, chash)

        # cap the ceiling by the caller's member tools[] (ADR-032/035 §5) — fail-closed for a
        # manifest_ref member too, whose registered manifest could otherwise declare a broader set.
        ext_ceiling = frozenset(capability_ceiling) if capability_ceiling is not None else None
        # Minted BEFORE the runnable is built so #728 provenance can carry it: an artifact this run
        # writes records the execution that produced it, and the instances are configured inside
        # _build_runnable. Generation is pure, so hoisting it changes nothing else.
        # #1072: the engine may supply its own id so it can cancel before any response arrives; the
        # service still mints one itself when the caller omits it.
        execution_id = execution_id if execution_id is not None else uuid.uuid4()

        # #1072 (design doc: "a duplicate id returns 409"): reject a REUSED id BEFORE the loop ever
        # runs, never after paying for a whole loop run and only then hitting a PK conflict. Two
        # checks, together covering both directions without leaking which org holds a foreign id:
        # an org-scoped read catches a same-org replay of an id whose lease was already released
        # (the terminal row still exists); the lease's own INSERT — a GLOBAL primary key, checked
        # below — catches an id still actively leased by any org, including another one.
        if self._leases is not None:
            if await self._executions.get(execution_id, org_id) is not None:
                raise DuplicateExecutionId(execution_id)
            await self._leases.create(execution_id, org_id)
        try:
            envelope, tool_specs, dispatch, llm, trust = await self._build_runnable(
                manifest,
                policy,
                org_id,
                external_ceiling=ext_ceiling,
                workspace_root=workspace_root,
                graph_id=graph_id,
                precedence_order=precedence_order,
                graph_authoritative=graph_authoritative,
                member_max_tokens=max_tokens,
                member_max_tool_calls=max_tool_calls,
                member_on_exhaustion=on_exhaustion,
                member_requires_valid_json=requires_valid_json,  # #853: one repair turn on bad JSON
                member_answer_from_tool=answer_from_tool,  # #900: a tool call that IS the answer
                # #961: the websites this run is held to
                required_sites=tuple(required_sites or ()),
                declared_output_keys=tuple(
                    declared_output_keys or ()
                ),  # #993: guarantee their shape
                producer=(
                    {**producer, "execution_id": str(execution_id)}
                    if producer is not None
                    else None
                ),
            )
            resource = f"harness_execution:{execution_id}"
            prompt = manifest.primary_prompt()
            # team-scope blackboard READ (#513): when a team member is bound to a graph and a reader
            # is wired, give the loop a fail-soft fetch of the team's current memory (the adopted
            # graph, scope=team for THIS team) to inject before the first LLM turn — concurrent +
            # cross-run visibility. None otherwise → the loop reasons exactly as before (zero
            # behaviour change).
            memory_context: Callable[[], Awaitable[str | None]] | None = None
            reader = self._memory_reader
            if reader is not None and team_id is not None and graph_id is not None:
                bound_graph, bound_team, bound_query = graph_id, team_id, user_input

                async def memory_context() -> str | None:
                    return await reader.team_context(
                        graph_id=bound_graph, team_id=bound_team, query=bound_query
                    )

            # #1072: run the loop as a cancellable task while a watcher polls the lease's cancel
            # flag on THIS (owning) replica — a `leases`-less service (pre-#1072 callers) runs the
            # loop directly, unchanged. On a cancellation `result` is a CANCELLED LoopResult built
            # from whatever `LoopProgress` had already booked, never a crash and never nothing.
            result = await self._run_loop_cancellable(
                execution_id=execution_id,
                organisation_id=org_id,
                llm=llm,
                system=prompt.body if prompt else "",
                user_input=user_input,
                tool_specs=tool_specs,
                dispatch=dispatch,
                policy=envelope,
                memory_context=memory_context,
                # Two trust sets, deliberately different sizes (#781): only the
                # knowledge-retriever connector emits `data_absent`, so its set is the narrower.
                citation_bindings=trust.citation,
                data_absent_bindings=trust.data_absence,
                # #961: which bindings the site-restriction gate fires on — resolved by the
                # registry, never taken from the manifest author's alias (#780's predicate).
                web_search_bindings=trust.web_search,
                # #975 (S2/ruling 6): both are caller-vouched provenance, trusted exactly as
                # `user_input` already is — they mint nothing beyond registry entries and pass the
                # SAME registration gate every other source does.
                prior_fetched_urls=prior_fetched_urls,
                person_supplied_text=effective_person_supplied_text,
            )
            row = await self._finish_execute(
                execution_id=execution_id,
                org_id=org_id,
                principal=principal,
                manifest=manifest,
                document=document,
                chash=chash,
                user_input=user_input,
                result=result,
                resource=resource,
                trace_id=trace_id,
                parent_execution_id=parent_execution_id,
                graph_id=graph_id,
                team_id=team_id,
                max_tokens=max_tokens,
                max_tool_calls=max_tool_calls,
                on_exhaustion=on_exhaustion,
                requires_valid_json=requires_valid_json,
                answer_from_tool=answer_from_tool,
                required_sites=required_sites,
                declared_output_keys=declared_output_keys,
            )
            return row
        finally:
            if self._leases is not None:
                await self._leases.release(execution_id, org_id)

    async def _run_loop_cancellable(
        self,
        *,
        execution_id: uuid.UUID,
        organisation_id: uuid.UUID,
        llm: LLMClient,
        **loop_kwargs: Any,
    ) -> LoopResult:
        """Run ``run_tool_use_loop`` (#1072 design doc). With no lease repository wired (the
        pre-#1072 shape) this is a plain awaited call, unchanged. With one wired, the loop runs as
        an ``asyncio.Task`` alongside a watcher that cancels ONLY the loop task when the lease's
        cancel flag is set — a cancellation is caught HERE, not propagated: the run is not a crash,
        it is a ``CANCELLED`` terminal built from whatever ``LoopProgress`` had already booked
        before the cut ("Spend survives cancellation")."""
        progress = LoopProgress()
        leases = self._leases
        if leases is None:
            try:
                return await run_tool_use_loop(llm=llm, progress=progress, **loop_kwargs)
            finally:
                await self._aclose_llm(llm)

        loop_task: asyncio.Task[LoopResult] = asyncio.create_task(
            run_tool_use_loop(llm=llm, progress=progress, **loop_kwargs)
        )
        watcher_requested_cancel = asyncio.Event()
        watcher_task = asyncio.create_task(
            self._watch_for_cancel(
                leases,
                execution_id,
                organisation_id,
                loop_task,
                self._cancel_poll_seconds,
                watcher_requested_cancel,
            )
        )
        try:
            try:
                return await loop_task
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                being_cancelled = current_task is not None and current_task.cancelling() > 0
                if watcher_requested_cancel.is_set() and not being_cancelled:
                    # The watcher asked for this cancel and nobody is cancelling the
                    # handler itself: this is a genuine user/timeout cancel of the run.
                    return LoopResult(
                        status=HarnessStatus.CANCELLED,
                        output=None,
                        steps=progress.steps,
                        iterations=progress.iterations,
                        total_tokens=progress.total_tokens,
                        input_tokens=progress.prompt_tokens,
                        output_tokens=progress.completion_tokens,
                        error_type="cancelled",
                        error_message="execution cancelled before it reached a terminal outcome",
                        served_citation_ids=progress.served_citation_ids,
                        protocol_shape=progress.protocol_shape,
                        fetched_urls=progress.fetched_urls,
                    )
                # Either the watcher never asked (something else cancelled loop_task),
                # or the handler task itself is being cancelled (shutdown, an outer
                # `asyncio.timeout()`/TaskGroup). Neither is a run cancel: finish
                # tearing the loop task down, then propagate so the caller sees the
                # real cancellation instead of a fabricated CANCELLED row.
                if not loop_task.done():
                    loop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await loop_task
                raise
        finally:
            watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher_task
            await self._aclose_llm(llm)

    @staticmethod
    async def _watch_for_cancel(
        leases: ExecutionLeaseRepository,
        execution_id: uuid.UUID,
        organisation_id: uuid.UUID,
        loop_task: asyncio.Task[Any],
        poll_seconds: float,
        requested_cancel: asyncio.Event,
    ) -> None:
        """Poll the lease's cancel flag on the OWNING replica (#1072 design doc) and cancel the
        loop task the moment it is set — this is what interrupts an in-flight LLM call. Polls with
        the RUN's own org (never org-blind: a wrong/unbound org sees zero rows under the lease
        table's forced RLS, so an org-blind poll could never observe its own run's flag). Sets
        ``requested_cancel`` BEFORE cancelling so the caller can tell a watcher-driven cancel apart
        from the handler task's own cancellation (#1072 PR #1094 review, blocker B1).

        A poll that raises (a DB blip) does not kill the watcher: that run would otherwise never be
        cancellable again for the rest of its execution (#1072 PR #1094 review, non-blocking note
        2). Log a warning keyed on the execution id only — never customer content — and keep
        polling. ``asyncio.CancelledError`` (the ``finally`` in ``_run_loop_cancellable`` cancelling
        this task) still propagates."""
        while not loop_task.done():
            try:
                cancel_requested = await leases.is_cancel_requested(execution_id, organisation_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "cancel-lease poll failed for execution %s; will retry", execution_id
                )
                await asyncio.sleep(poll_seconds)
                continue
            if cancel_requested:
                requested_cancel.set()
                loop_task.cancel()
                return
            await asyncio.sleep(poll_seconds)

    async def _finish_execute(
        self,
        *,
        execution_id: uuid.UUID,
        org_id: uuid.UUID,
        principal: Principal,
        manifest,  # noqa: ANN001
        document,  # noqa: ANN001
        chash: str | None,
        user_input: str,
        result: LoopResult,
        resource: str,
        trace_id: uuid.UUID | None,
        parent_execution_id: uuid.UUID | None,
        graph_id: str | None,
        team_id: str | None,
        max_tokens: int | None,
        max_tool_calls: int | None,
        on_exhaustion: Literal["escalate", "degrade"] | None,
        requires_valid_json: bool,
        answer_from_tool: str | None,
        required_sites: list[str] | None,
        declared_output_keys: list[str] | None,
    ) -> HarnessExecution:
        """Persist the terminal row + provenance + the post-run memory hook for one loop `result` —
        shared by the normal path and the #1072 CANCELLED path (`_run_loop_cancellable` builds a
        CANCELLED `LoopResult` from `LoopProgress`, so this is the SAME code either way: the memory
        hook already skips a non-terminal status, so CANCELLED needs no separate branch there)."""
        # #580 (ADR-021 never-silently): a retrieval reported data-absence and the member degraded
        # to PARTIAL — surface a structured degradation alert (not just a quiet PARTIAL result), so
        # an operator sees a from-scratch/empty-graph run proceeding without its expected data.
        if result.status is HarnessStatus.PARTIAL and result.error_type == "empty_retrieval":
            alert(
                Severity.WARNING,
                "retrieval.empty",
                "harness-runtime",
                "a member proceeded without retrieval data (data-absence) and was flagged PARTIAL",
                execution_id=str(execution_id),
                harness=str(manifest.metadata.id),
            )

        # A mid-loop HITL pause parks a resumable checkpoint; its id goes into the GATE step detail
        # (the engine correlates on the execution id, not this — it's for traceability, like a human
        # assignment). The transcript in the checkpoint is already redacted by the loop.
        steps = _serialize_steps(result.steps, protocol_shape=result.protocol_shape)
        cp = result.checkpoint
        # cp is not None whenever _is_hitl_pause holds (it just narrows cp for the create below)
        if _is_hitl_pause(result) and cp is not None:
            checkpoint = await self._checkpoints.create(
                organisation_id=org_id,
                execution_id=execution_id,
                manifest_doc=document,
                resume_messages=cp.messages,
                pending_tool_calls=cp.pending_tool_calls,
                approved_tool_call_id=cp.approved_tool_call_id,
                # #576: persist the member's per-member cap in the cursor (JSONB, no migration) so
                # resume re-applies the user's budget rather than reverting to the policy tier.
                resume_cursor=_cursor(
                    cp,
                    member_max_tokens=max_tokens,
                    member_max_tool_calls=max_tool_calls,
                    member_on_exhaustion=on_exhaustion,
                    # #853: the declaration AND the repair state, or the first pause of a declared
                    # member comes back undeclared and its earned repair slot is gone.
                    member_requires_valid_json=requires_valid_json,
                    # #900: the declared answer tool survives the pause the same way.
                    member_answer_from_tool=answer_from_tool,
                    json_repair_used=cp.json_repair_used,
                    json_repair_grant=cp.json_repair_grant,
                    output_repair_used=cp.output_repair_used,  # #1111
                    output_repair_grant=cp.output_repair_grant,
                    required_sites=tuple(required_sites or ()),  # #961: across the pause
                    # #993: across the pause
                    declared_output_keys=tuple(declared_output_keys or ()),
                ),
                redact_patterns=cp.redact_patterns,
            )
            if steps and steps[-1]["kind"] == StepKind.GATE.value:
                steps[-1]["detail"] = str(checkpoint.id)

        # Persist the durable run record FIRST, then emit provenance — an audit-emit failure must
        # never discard a run whose side effects (real registry executions) have already happened.
        row = await self._executions.create(
            execution_id=execution_id,
            organisation_id=org_id,
            user_id=principal.principal_id,
            harness_id=manifest.metadata.id,
            harness_name=manifest.metadata.name,
            content_hash=chash,
            status=result.status.value,
            input_text=user_input,
            output=result.output,
            error_type=result.error_type,
            error_message=result.error_message,
            iterations=result.iterations,
            total_tokens=result.total_tokens,
            model=_primary_model_binding(manifest),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            steps=steps,
            # run-tree correlation (#471): trace_id None at the root → repo mints it = execution_id.
            trace_id=trace_id,
            parent_execution_id=parent_execution_id,
            # #743 (§CITE): persist what the platform served, so rule 2 stays checkable after the
            # run. Without the durable record a fabricated citation is unfalsifiable once the loop
            # has exited — which is the whole gap #734 exposed.
            served_citation_ids=result.served_citation_ids,
            # #975 (§CITE cite-by-reference, A3): persisted on BOTH the success path and the
            # escalate/pause path — this single `create()` call handles both statuses.
            fetched_urls=result.fetched_urls,
            # #1111 decision 4: 1 + the recovery retries the member spent — read by the engine off
            # the execution response. (getattr: a loop-result double built before #1111 has none.)
            attempts=getattr(result, "attempts", 1),
        )
        await self._emit_provenance(
            result.steps,
            result.status.value,
            org_id=str(org_id),
            principal=str(principal.principal_id),
            resource=resource,
        )
        await self._emit_consciousness(
            org_id=str(org_id),
            principal=str(principal.principal_id),
            resource=resource,
            harness_name=manifest.metadata.name,
            status=result.status.value,
            summary=result.output,
        )
        # Post-run memory hook (#332 / ADR-027 §5): fire-and-forget AFTER the run is fully
        # persisted + audited — it can never fail, block, or slow the run. The ENTIRE block (arg
        # construction + schedule) is inside the swallow-all guard so that even an arg expression
        # (_tool_step_names / _manifest_graph_context / metadata access) raising on a future shape
        # change can never 500 an already-SUCCEEDED, already-persisted run.
        try:
            self._write_run_memories(
                harness_id=str(manifest.metadata.id),
                harness_name=manifest.metadata.name,
                status=result.status.value,
                user_input=user_input,
                output=result.output,
                tool_names=_tool_step_names(result.steps),
                execution_id=execution_id,
                # graph-adopt (#513): the run's BOUND graph (the user's adopted graph) is the team
                # blackboard — write there, never resolve_default_graph (no second graph). Fall back
                # to the manifest graph context only when the run carries no binding (legacy).
                graph_id=graph_id or _manifest_graph_context(manifest),
                team_id=team_id,
                # #554: thread the run's tool errors + rounds for the within-run classifier,
                # and read the consciousness.permissions posture (set → enrich; never-apply)
                tool_errors=_tool_step_errors(result.steps),
                rounds=result.iterations,
                record_pattern=manifest.governance.consciousness_permissions is not None,
                can_auto_apply=False,
            )
        except Exception:  # noqa: BLE001 — the run is done; the memory hook can never undo it
            logger.warning("post-run memory hook failed; run unaffected")
        return row

    async def cancel(
        self, *, execution_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> HarnessExecution | CancelPending:
        """Request cancellation of a run (#1072 design doc). A terminal row for
        ``(execution_id, organisation_id)`` already exists → returned untouched, idempotent — the
        lease is never consulted (a finished run's cancel never even looks at one). Otherwise a
        lease for the pair → the cancel flag is set and this waits up to ``cancel_wait_seconds`` for
        the terminal row to land, returning it if it does, else a :class:`CancelPending` (the route
        maps this to 202). An unknown id, or a lease belonging to a DIFFERENT org, both raise
        :class:`CancelError` (404) — identical in both cases, so a wrong-org caller learns nothing
        about whether the id exists at all; the flag is never set across orgs."""
        existing = await self._executions.get(execution_id, organisation_id)
        if existing is not None:
            return existing
        if self._leases is None:  # a service built without #1072 wiring cancels nothing in-flight
            raise CancelError("execution not found")
        requested = await self._leases.request_cancel(execution_id, organisation_id)
        if not requested:
            raise CancelError("execution not found")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._cancel_wait_seconds
        while True:
            row = await self._executions.get(execution_id, organisation_id)
            if row is not None:
                return row
            remaining = deadline - loop.time()
            if remaining <= 0:
                return CancelPending(execution_id=execution_id)
            await asyncio.sleep(min(self._cancel_poll_seconds, remaining))

    async def resume(
        self,
        *,
        execution_id: uuid.UUID,
        principal: Principal,
        decision: str,
        decision_reason: str | None = None,
    ) -> HarnessExecution:
        """Resolve a mid-loop HITL pause. APPROVED re-sources the exact paused manifest, rebuilds
        the runnable, and re-runs the loop from the checkpoint (the approved tool bypasses the gate
        once); ``DENIED`` terminates the run FAILED (``human_rejected``). Updates the SAME execution
        row in place and emits provenance only for the new step tail. Fail-closed throughout."""
        if principal.organisation_id is None:
            raise ResumeError("authenticated principal has no organisation scope", 401)
        org_id = principal.organisation_id
        execution = await self._executions.get(execution_id, org_id)
        if execution is None:
            raise ResumeError("execution not found", 404)
        if (
            execution.status != HarnessStatus.ESCALATED.value
            or execution.error_type != "hitl_required"
        ):
            raise ResumeError("execution is not awaiting HITL approval", 409)
        checkpoint = await self._checkpoints.get_latest_pending(execution_id, org_id)
        if checkpoint is None:
            raise ResumeError("no pending checkpoint to resume", 409)
        if decision not in ("APPROVED", "DENIED"):
            raise ResumeError("decision must be APPROVED or DENIED", 422)

        resource = f"harness_execution:{execution_id}"
        prov = str(principal.principal_id)
        # CAS the decision so a concurrent approve applies exactly once.
        if await self._checkpoints.set_decision(checkpoint.id, org_id, decision) is None:
            raise ResumeError("checkpoint already decided", 409)

        # If applying the decision then fails (e.g. the registry/LLM is transiently down while
        # rebuilding the runnable), un-claim the checkpoint so the run stays retryable rather than
        # stranded ESCALATED with a no-longer-PENDING checkpoint. (A failure AFTER the loop already
        # dispatched a tool may re-run it on retry — the common failure is pre-loop in build.)
        try:
            if decision == "DENIED":
                return await self._resume_denied(execution, org_id, prov, resource, decision_reason)
            return await self._resume_approved(
                execution, checkpoint, org_id, prov, resource, decision_reason
            )
        except Exception:
            await self._checkpoints.revert_to_pending(checkpoint.id, org_id)
            raise

    async def _resume_denied(
        self,
        execution: HarnessExecution,
        org_id: uuid.UUID,
        prov: str,
        resource: str,
        reason: str | None,
    ) -> HarnessExecution:
        prior = list(execution.steps or [])
        message = reason or "rejected by human"
        steps = [
            *prior,
            {
                "index": len(prior),
                "kind": StepKind.GATE.value,
                "name": "hitl",
                "status": "denied",
                "detail": message[:500],
            },
        ]
        row = await self._executions.update_run(
            execution.id,
            org_id,
            status=HarnessStatus.FAILED.value,
            output=execution.output,
            error_type="human_rejected",
            error_message=message,
            iterations=execution.iterations,
            total_tokens=execution.total_tokens,
            steps=steps,
        )
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=prov,
                action="human.reject",
                resource=resource,
                outcome="hitl:denied",
            )
        )
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=prov,
                action="harness.resume",
                resource=resource,
                outcome=HarnessStatus.FAILED.value,
            )
        )
        # Post-run memory hook: the run COMPLETED (FAILED, human_rejected) and the denial reason is
        # explicit human feedback. No manifest is loaded on this path → no graph context (the KGS
        # org-default memory graph applies). The whole block is guarded so no arg expression can
        # raise into a run that has already FAILED-and-persisted.
        try:
            self._write_run_memories(
                harness_id=str(getattr(execution, "harness_id", "") or ""),
                harness_name=execution.harness_name,
                status=HarnessStatus.FAILED.value,
                user_input=execution.input,
                output=message,
                tool_names=[],
                execution_id=execution.id,
                graph_id=None,
                human_feedback=reason,
            )
        except Exception:  # noqa: BLE001 — the run is done; the memory hook can never undo it
            logger.warning("post-run memory hook failed; run unaffected")
        return row or execution

    async def _resume_approved(
        self,
        execution: HarnessExecution,
        checkpoint,  # noqa: ANN001 — the HarnessCheckpoint row
        org_id: uuid.UUID,
        prov: str,
        resource: str,
        decision_reason: str | None = None,
    ) -> HarnessExecution:
        # Replay the EXACT paused manifest (stored on the checkpoint → no drift, hash stable).
        document = checkpoint.manifest_doc
        manifest = load_ohm(document)
        policy = resolve_policy_set(self._force_policy_set or manifest.governance.policy_set_ref)
        # Re-enforce the load-time gates on resume: the manifest can't have drifted, but the policy
        # floor may have tightened since the pause — a now-forbidden capability/provider/signature
        # must fail-closed here exactly as a fresh execute() would, not slip through on resume.
        verify_signatures(
            document, self._trust, require=self._require_signature or policy.require_signature
        )
        enforce_load_policy(manifest, policy)
        cursor = checkpoint.resume_cursor
        # #576: restore the member's per-member cap from the checkpoint so resume keeps the user's
        # budget. Without it, resume reverts to the policy tier and a heavy member already past it
        # would re-escalate immediately — the exact bug #576 fixes. Old checkpoints lack the keys
        # → None → the tier (a pre-#576 paused run is unchanged).
        envelope, tool_specs, dispatch, llm, trust = await self._build_runnable(
            manifest,
            policy,
            org_id,
            member_max_tokens=cursor.get("member_max_tokens"),
            member_max_tool_calls=cursor.get("member_max_tool_calls"),
            member_on_exhaustion=cursor.get("member_on_exhaustion"),  # #587: re-apply on resume
            # #853: re-apply on resume. Old checkpoints lack the key → False → unchanged.
            member_requires_valid_json=bool(cursor.get("member_requires_valid_json")),
            # #900: re-apply the declared answer tool. Old checkpoints lack the key → None →
            # unchanged (a pre-#900 paused run resumes exactly as it does today).
            member_answer_from_tool=cursor.get("member_answer_from_tool"),
            # #961: re-apply the run's site restriction. Old checkpoints lack the key → () → a
            # pre-#961 paused run is unchanged.
            required_sites=tuple(cursor.get("required_sites") or ()),
            # #993: re-apply the run's declared output keys. Old checkpoints lack the key → () → a
            # pre-#993 paused run is unchanged.
            declared_output_keys=tuple(cursor.get("declared_output_keys") or ()),
        )
        resume_state = LoopCheckpoint(
            messages=checkpoint.resume_messages,
            pending_tool_calls=checkpoint.pending_tool_calls,
            approved_tool_call_id=checkpoint.approved_tool_call_id,
            iteration=cursor["iteration"],
            tool_calls_made=cursor["tool_calls_made"],
            tokens_used=cursor["tokens_used"],
            redact_patterns=checkpoint.redact_patterns,
            # #853: old checkpoints lack the keys → False/0 → a pre-#853 paused run is unchanged.
            json_repair_used=bool(cursor.get("json_repair_used")),
            json_repair_grant=int(cursor.get("json_repair_grant") or 0),
            # #1111: same default-safe read — a pre-#1111 checkpoint resumes with the correction
            # unspent.
            output_repair_used=bool(cursor.get("output_repair_used")),
            output_repair_grant=int(cursor.get("output_repair_grant") or 0),
            # #1111 decision 4: a pre-#1111 checkpoint lacks the key → 0 retries carried.
            recovery_retries=int(cursor.get("recovery_retries") or 0),
        )
        prompt = manifest.primary_prompt()
        try:
            result = await run_tool_use_loop(
                llm=llm,
                system=prompt.body if prompt else "",
                user_input=execution.input,
                tool_specs=tool_specs,
                dispatch=dispatch,
                policy=envelope,
                resume_state=resume_state,
                # Two trust sets, deliberately different sizes (#781): only the
                # knowledge-retriever connector emits `data_absent`, so its set is the narrower.
                citation_bindings=trust.citation,
                data_absent_bindings=trust.data_absence,
                # #961: which bindings the site-restriction gate fires on — resolved by the
                # registry, never taken from the manifest author's alias (#780's predicate).
                web_search_bindings=trust.web_search,
                # #782 (§CITE): the answer-time gate checks against the PERSISTED UNION, not this
                # segment alone. The loop's own served set is a FRESH list on a HITL resume (the
                # checkpoint carries the transcript, not platform counters), so without this a
                # post-pause answer citing a pre-pause source is failed by bookkeeping — a correct
                # answer blocked. The fresh-run call site needs nothing: it has no prior segment.
                prior_served_citation_ids=execution.served_citation_ids or [],
                # #975 (A3): the persisted UNION, exactly the `prior_served_citation_ids` pattern —
                # the loop seeds these first, then unions in the transcript's own re-derivation, so
                # no already-numbered `[Sn]` marker from before the pause ever renumbers.
                prior_fetched_urls=execution.fetched_urls or [],
            )
        finally:
            await self._aclose_llm(llm)

        # Append the NEW segment's steps to the prior trace; the loop reset its step list on resume,
        # so result.steps is the new tail only — provenance below emits only that, never the prefix.
        prior = list(execution.steps or [])
        new_steps = _serialize_steps(
            result.steps, base=len(prior), protocol_shape=result.protocol_shape
        )
        new_cp = result.checkpoint
        # a chained gate → park a fresh checkpoint (new_cp is not None whenever _is_hitl_pause)
        if _is_hitl_pause(result) and new_cp is not None:
            cp2 = await self._checkpoints.create(
                organisation_id=org_id,
                execution_id=execution.id,
                manifest_doc=checkpoint.manifest_doc,
                resume_messages=new_cp.messages,
                pending_tool_calls=new_cp.pending_tool_calls,
                approved_tool_call_id=new_cp.approved_tool_call_id,
                # #576: carry the cap forward across a CHAINED gate so the second pause/resume keeps
                # it too (a heavy member can cross more than one HITL gate).
                resume_cursor=_cursor(
                    new_cp,
                    member_max_tokens=cursor.get("member_max_tokens"),
                    member_max_tool_calls=cursor.get("member_max_tool_calls"),
                    member_on_exhaustion=cursor.get("member_on_exhaustion"),
                    member_requires_valid_json=bool(
                        cursor.get("member_requires_valid_json")
                    ),  # #853: across a chained gate
                    # #900: across a chained gate, same as every other declaration above.
                    member_answer_from_tool=cursor.get("member_answer_from_tool"),
                    json_repair_used=new_cp.json_repair_used,
                    json_repair_grant=new_cp.json_repair_grant,
                    output_repair_used=new_cp.output_repair_used,  # #1111
                    output_repair_grant=new_cp.output_repair_grant,
                    required_sites=tuple(cursor.get("required_sites") or ()),  # #961: chained gate
                    declared_output_keys=tuple(
                        cursor.get("declared_output_keys") or ()
                    ),  # #993: across a chained gate
                ),
                redact_patterns=new_cp.redact_patterns,
            )
            if new_steps and new_steps[-1]["kind"] == StepKind.GATE.value:
                new_steps[-1]["detail"] = str(cp2.id)
        row = await self._executions.update_run(
            execution.id,
            org_id,
            status=result.status.value,
            output=result.output
            or execution.output,  # keep the prior partial on a chained re-pause
            error_type=result.error_type,
            error_message=result.error_message,
            iterations=result.iterations,  # cumulative — the cursor carried the prior iteration
            total_tokens=result.total_tokens,  # cumulative — the cursor seeded prior tokens
            # The cursor carries only the cumulative total, so the loop's input/output reflect this
            # segment only; fold them into the prior persisted split to keep the breakdown
            # cumulative.
            input_tokens=(execution.input_tokens or 0) + result.input_tokens,
            output_tokens=(execution.output_tokens or 0) + result.output_tokens,
            steps=[*prior, *new_steps],
            # #743 (§CITE): the loop's served set covers the post-resume segment only, so the
            # repository UNIONS it into what the pre-pause segment already recorded. A citation
            # served before the pause is still one this member was handed.
            served_citation_ids=result.served_citation_ids,
            # #975 (§CITE cite-by-reference, A3): same UNION posture — the repository owns merging
            # this segment's registry into what the pre-pause segment already recorded.
            fetched_urls=result.fetched_urls,
            # #1111 decision 4: cumulative — the cursor carried the pre-pause recovery retries.
            attempts=getattr(result, "attempts", 1),
        )
        await self._emit_provenance(
            result.steps,  # the new tail only
            result.status.value,
            org_id=str(org_id),
            principal=prov,
            resource=resource,
            closure_action="harness.resume",
        )
        await self._emit_consciousness(
            org_id=str(org_id),
            principal=prov,
            resource=resource,
            harness_name=execution.harness_name,
            status=result.status.value,
            summary=result.output,
        )
        # Post-run memory hook: only a COMPLETED run writes (a chained re-pause does not); an
        # approval carrying an explicit reason is human feedback worth a procedural memory. The
        # whole block is guarded so no arg expression can raise into an already-persisted run.
        try:
            self._write_run_memories(
                harness_id=str(manifest.metadata.id),
                harness_name=execution.harness_name,
                status=result.status.value,
                user_input=execution.input,
                output=result.output,
                tool_names=_tool_step_names(result.steps),
                execution_id=execution.id,
                graph_id=_manifest_graph_context(manifest),
                human_feedback=decision_reason,
                tool_errors=_tool_step_errors(result.steps),  # #554: see the leaf-run hook above
                rounds=result.iterations,
                record_pattern=manifest.governance.consciousness_permissions is not None,
                can_auto_apply=False,
            )
        except Exception:  # noqa: BLE001 — the run is done; the memory hook can never undo it
            logger.warning("post-run memory hook failed; run unaffected")
        return row or execution

    def _write_run_memories(
        self,
        *,
        harness_id: str,
        harness_name: str,
        status: str,
        user_input: str,
        output: str | None,
        tool_names: list[str],
        execution_id: uuid.UUID,
        graph_id: str | None,
        team_id: str | None = None,
        human_feedback: str | None = None,
        tool_errors: list[str] | None = None,
        rounds: int = 0,
        record_pattern: bool = False,
        can_auto_apply: bool = False,
    ) -> None:
        """The flag-gated, fail-soft post-run memory hook (#332 / ADR-027 §5).

        No writer (flag off) → ZERO calls. A completed run (SUCCEEDED/FAILED) schedules one
        episodic outcome memory; explicit human feedback additionally schedules a procedural one.
        A team run (``team_id`` set, #513) writes them ``scope=team`` under the team identity so the
        team's members + future runs share the blackboard; else ``scope=agent``. Scheduling is
        fire-and-forget (≈2s-timeout detached tasks) and everything is swallowed — this method can
        never raise into the run path.
        """
        if self._memory is None or status not in _MEMORY_TERMINAL_STATUSES:
            return
        try:
            self._memory.schedule_run_outcome(
                harness_id=harness_id,
                harness_name=harness_name,
                status=status,
                user_input=user_input,
                output=output,
                tool_names=tool_names,
                execution_id=execution_id,
                graph_id=graph_id,
                team_id=team_id,
                tool_errors=tool_errors,
                rounds=rounds,
                record_pattern=record_pattern,
                can_auto_apply=can_auto_apply,
            )
            if human_feedback and human_feedback.strip():
                self._memory.schedule_human_feedback(
                    harness_id=harness_id,
                    harness_name=harness_name,
                    feedback=human_feedback,
                    execution_id=execution_id,
                    graph_id=graph_id,
                    team_id=team_id,
                )
        except Exception:  # noqa: BLE001 — belt-and-braces; the writer already swallows everything
            logger.warning("post-run memory hook failed to schedule; run unaffected")

    async def _build_runnable(
        self,
        manifest,  # noqa: ANN001
        policy,  # noqa: ANN001
        org_id: uuid.UUID,
        *,
        external_ceiling: frozenset[str] | None = None,
        workspace_root: str | None = None,
        graph_id: str | None = None,
        precedence_order: list[str] | None = None,
        graph_authoritative: bool = False,
        member_max_tokens: int | None = None,
        member_max_tool_calls: int | None = None,
        member_on_exhaustion: Literal["escalate", "degrade"] | None = None,
        member_requires_valid_json: bool = False,
        member_answer_from_tool: str | None = None,
        required_sites: tuple[str, ...] = (),
        declared_output_keys: tuple[str, ...] = (),
        producer: dict[str, Any] | None = None,
    ) -> tuple[Any, list[ToolSpec], Any, LLMClient, TrustedBindings]:
        """Resolve + materialise the manifest's capabilities, build the dispatch + the LLM + the
        runtime envelope. Shared by execute() and resume() so a resume sets up identically.
        ``external_ceiling`` caps the ceiling by the caller's member ``tools[]`` (ADR-032).
        ``workspace_root`` (#518) is set on each file-tool instance's config → file-native."""
        resolved = await self._resolve_all(manifest)
        envelope = build_envelope(
            manifest,
            policy,
            hard_max_iterations=self._max_iterations,
            external_ceiling=external_ceiling,
            member_max_tokens=member_max_tokens,
            member_max_tool_calls=member_max_tool_calls,
            max_tokens_ceiling=self._max_tokens_per_member_ceiling,
            max_tool_calls_ceiling=self._max_tool_calls_per_member_ceiling,
            member_on_exhaustion=member_on_exhaustion,  # #587: degrade vs escalate at a budget gate
            member_requires_valid_json=member_requires_valid_json,  # #853: one JSON repair turn
            member_answer_from_tool=member_answer_from_tool,  # #900: a tool call that IS the answer
            required_sites=required_sites,  # #961: the websites this run is held to
            declared_output_keys=declared_output_keys,  # #993: guarantee their shape on the way out
        )
        instance_by_binding, tool_specs = await self._materialise(
            manifest,
            resolved,
            workspace_root=workspace_root,
            graph_id=graph_id,
            precedence_order=precedence_order,
            graph_authoritative=graph_authoritative,
            producer=producer,
        )

        async def dispatch(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
            instance_id = instance_by_binding.get(spec.binding)
            if instance_id is None:  # invariant: every emitted tool_spec.binding was materialised
                raise RegistryError(f"no instance for capability binding {spec.binding!r}")
            # #956 ruling 1: the BOUND operation is what runs. `{"operation": ..., **args}` let a
            # model-supplied key spread over the literal and pick the operation itself; the
            # payload builder strips an equal key and refuses a differing one before the
            # registry is ever called. The refusal propagates to the loop's dispatch `except`,
            # which feeds it back to the model as a coded tool error and lets the run go on.
            try:
                payload = dispatch_payload(spec, args)
            except OperationOverrideRefused as exc:
                logger.warning(
                    "tool %s: model-supplied operation refused, bound operation %r wins "
                    "(supplied, first %d chars: %s)",
                    exc.tool,
                    exc.bound,
                    len(exc.supplied_preview),
                    exc.supplied_preview,
                )
                raise
            except ToolDispatchRefused as exc:
                # #1004 item 4: the other refusal the payload builder can raise (arguments that
                # were not a JSON object). A refusal that leaves no trace is worse than one that
                # does; the class name is ours, so nothing model-authored is logged.
                logger.warning(
                    "tool %s: dispatch refused before the registry (%s)",
                    spec.name,
                    type(exc).__name__,
                )
                raise
            execution = await self._registry.execute(instance_id, payload)
            if execution.get("status") != "SUCCESS":
                raise _tool_execution_error(execution)
            return execution.get("output_data") or {}

        trust = _trusted_bindings(manifest, resolved)

        llm = await self._build_llm(manifest, org_id)
        return envelope, tool_specs, dispatch, llm, trust

    @staticmethod
    async def _aclose_llm(llm: LLMClient) -> None:
        aclose = getattr(llm, "aclose", None)
        if aclose is not None:
            await aclose()

    async def _dispatch_human(
        self,
        manifest,  # noqa: ANN001
        actor,  # noqa: ANN001
        org_id: uuid.UUID,
        principal: Principal,
        user_input: str,
        chash: str,
    ) -> HarnessExecution:
        """Human entrypoint actor → a task-board assignment + an ESCALATED run (resume is R5)."""
        execution_id = uuid.uuid4()
        resource = f"harness_execution:{execution_id}"
        human_role = actor.human_role or actor.role
        assignment = await self._assignments.create(
            organisation_id=org_id,
            execution_id=execution_id,
            harness_id=manifest.metadata.id,
            human_role=human_role,
            input_text=user_input,
        )
        row = await self._executions.create(
            execution_id=execution_id,
            organisation_id=org_id,
            user_id=principal.principal_id,
            harness_id=manifest.metadata.id,
            harness_name=manifest.metadata.name,
            content_hash=chash,
            status=HarnessStatus.ESCALATED.value,
            input_text=user_input,
            output=f"assigned to human role {human_role!r} (assignment {assignment.id})",
            error_type="human_assignment",
            error_message=f"awaiting human role {human_role!r}",
            iterations=0,
            total_tokens=0,
            steps=[
                {
                    "index": 0,
                    "kind": StepKind.GATE.value,
                    "name": human_role,
                    "status": "assigned",
                    "detail": str(assignment.id),
                }
            ],
        )
        prov = str(principal.principal_id)
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=prov,
                action="human.assign",
                resource=resource,
                outcome=f"{human_role}:assigned",
            )
        )
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=prov,
                action="harness.execute",
                resource=resource,
                outcome=HarnessStatus.ESCALATED.value,
            )
        )
        await self._emit_consciousness(
            org_id=str(org_id),
            principal=prov,
            resource=resource,
            harness_name=manifest.metadata.name,
            status=HarnessStatus.ESCALATED.value,
            summary=f"assigned to human role {human_role!r}",
        )
        return row

    async def _build_llm(self, manifest, org_id: uuid.UUID) -> LLMClient:  # noqa: ANN001
        """Build the loop's LLM client: the key-free fake, or a live client from the OHM's primary
        model + a BYOM key resolved via the broker (ADR-008 — no platform fallback)."""
        if self._llm_mode == "fake":
            return build_fake_client()
        model = manifest.primary_model()
        if model is None:
            raise HarnessExecutionError("live LLM mode requires a model in the OHM")
        provider, _, model_id = model.binding.partition("/")
        if not model_id:
            raise HarnessExecutionError(
                f"model binding {model.binding!r} must be '<provider>/<model-id>'"
            )
        credential_id = model.config.get("credential_id")
        if not credential_id:
            raise HarnessExecutionError("live LLM requires a BYOM key (model.config.credential_id)")
        try:
            payload = await self._broker.resolve_credential(
                credential_id=str(credential_id), organisation_id=org_id
            )
        except BrokerError as exc:
            raise HarnessExecutionError(f"model credential resolution failed: {exc}") from exc
        api_key = payload.get("api_key") or payload.get("key")
        if not api_key:
            raise HarnessExecutionError("the resolved model credential has no api_key")

        # Resolve the base URL: the CONNECTION's own `base_url` (a custom OpenAI-compatible
        # endpoint, e.g. a local or self-hosted LLM) wins; otherwise fall back to the operator's
        # server map keyed by provider (openrouter/openai). A USER-supplied URL is attacker-
        # controllable → run it through the egress guard (SSRF). The operator's server-map URLs are
        # TRUSTED and are NOT guarded.
        connection_base_url = payload.get("base_url")
        base_url = connection_base_url or self._llm_base_urls.get(provider)
        if not base_url:
            raise HarnessExecutionError(
                f"no base URL for provider {provider!r} — set base_url on the connection or "
                "configure the provider"
            )
        if connection_base_url:
            try:
                validate_outbound_url(
                    str(connection_base_url), allow_private=self._llm_allow_private
                )
            except EgressBlockedError as exc:
                raise HarnessExecutionError(f"connection base_url rejected: {exc}") from exc
        try:
            return build_live_client(
                protocol_shape=model.protocol_shape,
                base_url=base_url,
                api_key=str(api_key),
                model=model_id,
                timeout=self._llm_timeout,
            )
        except LLMConfigError as exc:
            raise HarnessExecutionError(str(exc)) from exc

    async def _source_document(
        self, manifest_inline: str | dict[str, Any] | None, manifest_ref: str | None
    ) -> dict[str, Any]:
        """Produce the OHM document from inline YAML/object or a registered harness reference."""
        if manifest_ref is not None:
            try:
                item = await self._registry.get_capability(manifest_ref)
            except RegistryError as exc:
                raise OHMReferenceError(
                    f"manifest_ref {manifest_ref!r} not resolvable: {exc}"
                ) from exc
            if item.get("kind") != "harness":
                raise OHMReferenceError(
                    f"manifest_ref {manifest_ref!r} is a {item.get('kind')!r}, not a harness"
                )
            document = item.get("descriptor")
        elif isinstance(manifest_inline, str):
            try:
                document = yaml.safe_load(manifest_inline)
            except yaml.YAMLError as exc:
                raise OHMParseError(f"OHM YAML is malformed: {exc}") from exc
        else:
            document = manifest_inline
        if not isinstance(document, dict):
            raise OHMParseError("OHM document must be a mapping at the top level")
        return document

    async def _resolve_all(self, manifest) -> dict[str, dict[str, Any]]:  # noqa: ANN001
        async def resolve(ref: str, explicit_id: str | None) -> dict[str, Any]:
            return await self._registry.resolve_capability(ref, explicit_id=explicit_id)

        return await resolve_capabilities(manifest, resolve)  # OHMReferenceError → 422

    async def _materialise(
        self,
        manifest,
        resolved: dict[str, dict[str, Any]],  # noqa: ANN001
        *,
        workspace_root: str | None = None,
        graph_id: str | None = None,
        precedence_order: list[str] | None = None,
        graph_authoritative: bool = False,
        producer: dict[str, Any] | None = None,
    ) -> tuple[dict[str, uuid.UUID], list[ToolSpec]]:
        """Find-or-create a registry instance per capability + build the agent's full toolset.

        Idempotent: each capability maps to a deterministically-named instance
        (``harness:<id>:<binding>``), reused across runs rather than recreated — so retries don't
        accumulate instances and a partial setup failure has a bounded, reusable footprint (the
        registry has no instance-delete endpoint to compensate-delete against). Tool names are
        ``<binding>__<operation>``; bindings are load-time-unique (parse), so they never collide.

        #663 — a capability whose credential mapping is load-bearing (``api_key`` /
        ``connection_string`` / ``username_password``; OAuth is broker-resolved per request and
        exempt) never gets a fresh unconfigurable mint. When neither the deterministic instance nor
        the manifest's ``credential_mappings`` can satisfy it, the run binds the org's own
        configured instance of the same capability instead — the one the user set up in the
        console — and fails fast (before any model tokens) when the org has none, naming the
        binding and the missing types. A reused ORG instance keeps its own configuration — it is
        the tool the user set up, not this run's — and the per-run keys are read only by
        first-party keyless connectors, which never take that branch.

        #1130 — a reused DETERMINISTIC instance is different: it is this harness's own instance,
        and a seeded app's sub-harness id is stable across runs, so the same instance is picked up
        on every later run. Its stored configuration is rebound to the CURRENT run's per-run keys
        (merged over what is there, then pushed back to the registry) before anything dispatches;
        otherwise every later run's artifacts are filed under the run that minted it first.
        """
        instance_by_binding: dict[str, uuid.UUID] = {}
        tool_specs: list[ToolSpec] = []
        seen_tools: set[str] = set()
        per_run = _per_run_configuration(
            workspace_root=workspace_root,
            graph_id=graph_id,
            precedence_order=precedence_order,
            graph_authoritative=graph_authoritative,
            producer=producer,
        )
        try:
            rows = await self._registry.list_instances()
            existing = {i.get("name"): i for i in rows}
            for cap in manifest.capabilities:
                item = resolved[cap.binding]
                name = f"harness:{manifest.metadata.id}:{cap.binding}"
                needed = _mapped_credential_types(item.get("descriptor") or {})
                mappings = cap.config.get("credential_mappings") or {}
                reused_mappings: dict[str, str] = {}  # a fresh create has nothing to preserve
                prior = existing.get(name)
                if (
                    prior is not None
                    and str(prior.get("capability_id")) == str(item["id"])
                    # a pre-#663 junk prior (same name, unconfigurable) must not be re-selected —
                    # unless the manifest's mappings will configure it right below
                    and all(
                        t in (prior.get("credential_mappings") or {}) or t in mappings
                        for t in needed
                    )
                ):
                    instance_id = uuid.UUID(str(prior["id"]))
                    reused_mappings = prior.get("credential_mappings") or {}
                    # #911: the reused instance's own configuration is what it will dispatch
                    # with — carry it into the model-facing schema below (see the comment at the
                    # `tool_specs_for` call for why).
                    bound_config: dict[str, Any] = prior.get("configuration") or {}
                    # #1130: that stored configuration was written by whichever run minted this
                    # instance, and a seeded app's sub-harness id is the SAME on every run, so
                    # without this the second and every later run writes its artifacts under the
                    # first run's producer/graph/working tree. Merge — never replace — so keys the
                    # manifest or the org authored survive; push it back, because the dispatch
                    # reads the registry's PERSISTED row, not anything computed here. A failure
                    # raises RegistryError out of this block, which fails the whole setup: the run
                    # never dispatches under a stale identity.
                    if per_run and any(bound_config.get(k) != v for k, v in per_run.items()):
                        bound_config = {**bound_config, **per_run}
                        await self._registry.update_configuration(instance_id, bound_config)
                elif needed and not all(t in mappings for t in needed):
                    # #663: a fresh mint could never be configured (creation takes no credentials
                    # and nothing here could bind them) — bind the org's configured instance.
                    sibling = next(
                        (
                            r
                            for r in rows
                            if str(r.get("capability_id")) == str(item["id"])
                            and all(t in (r.get("credential_mappings") or {}) for t in needed)
                        ),
                        None,
                    )
                    if sibling is None:
                        missing = [t for t in needed if t not in mappings]
                        raise HarnessExecutionError(
                            f"capability {cap.binding!r} requires credential types {missing} but "
                            "the organisation has no configured instance of it — connect the "
                            "credential and configure the tool before running"
                        )
                    instance_id = uuid.UUID(str(sibling["id"]))
                    reused_mappings = sibling.get("credential_mappings") or {}
                    # #911: same reasoning as the deterministic-reuse branch above — the sibling's
                    # own configuration is what this run will actually dispatch with.
                    bound_config = sibling.get("configuration") or {}
                else:
                    cap_config = {
                        k: v for k, v in cap.config.items() if k not in _RESERVED_CONFIG_KEYS
                    }
                    # bind this run's own keys onto the new instance (see
                    # `_per_run_configuration` for what each one is for and who reads it).
                    cap_config.update(per_run)
                    instance = await self._registry.create_instance(
                        capability_id=str(item["id"]),
                        name=name,
                        configuration=cap_config,
                    )
                    instance_id = uuid.UUID(str(instance["id"]))
                    # #911: the fresh mint's effective configuration is this same merged dict —
                    # not a second, independently-built one.
                    bound_config = cap_config
                instance_by_binding[cap.binding] = instance_id
                if mappings:
                    # configure-credentials REPLACES the whole map in the registry, so a reused
                    # (already-configured) instance must receive the MERGE — the manifest's partial
                    # mappings must never destroy the org instance's own coverage (#663 review).
                    await self._registry.configure_credentials(
                        instance_id, {**reused_mappings, **mappings}
                    )
                descriptor = _project_resolved_schema(
                    item.get("descriptor") or {}, cap.resolved_schema
                )
                # #911: the dispatching instance's effective configuration (whichever of the three
                # branches above supplied it) must agree with what the model is asked for — a
                # required argument already bound on the instance is dropped from the projected
                # `required` list without hiding the property. Outbound mirror of the registry's
                # own inbound check at
                # capability-registry-service/.../domain/executors/input_validation.py:92-98.
                specs = tool_specs_for(cap.binding, descriptor, bound_config=bound_config)
                # #698 AC6: a resolved kind=tool capability that yields no callable spec is a
                # BROKEN binding, not an empty one. This is how the whole #698 chain stayed
                # invisible — an imported MCP tool stored no operations, the model was handed an
                # empty tool list, it invented an answer, and the step trace looked normal while
                # the user paid for real tokens. Fail here, before the LLM is built. Scoped to an
                # explicit kind=tool in the DESCRIPTOR: knowledge, graph and retriever bindings
                # legitimately emit no ToolSpec, and DescriptorKind has no 'knowledge' member, so
                # the registry row's own kind column cannot tell them apart.
                if not specs and descriptor.get("kind") == "tool":
                    raise HarnessExecutionError(
                        f"capability {cap.binding!r} resolved to a tool that declares no callable "
                        "operation — re-import it, or fix its descriptor, before running"
                    )
                for spec in specs:
                    if (
                        spec.name in seen_tools
                    ):  # de-dup duplicate operation names within a descriptor
                        continue
                    seen_tools.add(spec.name)
                    tool_specs.append(spec)
        except RegistryError as exc:
            raise HarnessExecutionError(f"capability setup failed: {exc}") from exc
        return instance_by_binding, tool_specs

    async def _emit_provenance(
        self,
        steps: list[LoopStep],
        status: str,
        *,
        org_id: str,
        principal: str,
        resource: str,
        closure_action: str = "harness.execute",
    ) -> None:
        """One provenance event per step + a closure event (the single write-through path). On a
        resume, ``steps`` is the new segment only (the loop reset its trace), so the replayed prefix
        is never re-emitted — preserving the per-step audit ordering across the pause."""
        for step in steps:
            action = provenance_action_for(step.kind, step.status)
            # coalesce so a model-supplied (possibly empty) tool name can't fail the required-field
            # contract on the substrate collector.
            outcome = f"{step.name or '<unnamed>'}:{step.status or 'unknown'}"
            await self._provenance.emit(
                ProvenanceRecord(
                    organisation_id=org_id,
                    principal=principal,
                    action=action,
                    resource=resource,
                    outcome=outcome,
                )
            )
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=org_id,
                principal=principal,
                action=closure_action,
                resource=resource,
                outcome=status,
            )
        )

    async def _emit_consciousness(
        self,
        *,
        org_id: str,
        principal: str,
        resource: str,
        harness_name: str,
        status: str,
        summary: str | None,
    ) -> None:
        """Write-through a consciousness record (a provenance/event hook, NOT a privileged path) —
        captures the run's outcome so future interactions can retrieve it (the retrieval side is a
        later capability). Emitted via the same single provenance write path."""
        note = (summary or "").strip().replace("\n", " ")[:200] or "(no output)"
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=org_id,
                principal=principal,
                action="consciousness.write",
                resource=resource,
                outcome=f"{harness_name} → {status}: {note}",
            )
        )
