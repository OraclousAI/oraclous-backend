"""#724 — a model call over customer data uses the ORG's credential, never a platform key.

KGS builds six OpenAI-compatible clients from ``settings.openai_api_key`` (``KGS_OPENAI_API_KEY``),
a value compose injects at deploy time. Every one of them reads customer data: entity extraction
reads each chunk of each ingested document, and the other five read documents, images, schemas and
code. The org's own broker credential is never consulted, so the customer neither chooses the model
that reads their data nor sees what it costs, and ADR-008 §3.6 operator separation is at risk in
cloud mode.

knowledge-retriever-service already solved this (ADR-037): ``resolve_byom_judge`` resolves a
per-request credential from the broker, org-scoped, and fails closed. KGS never adopted it, and it
already owns a working broker client (``services/credential_client.py``, built for #653).

The decision recorded on #724: resolve an ORG DEFAULT model credential, overridable PER GRAPH, and
fail closed with a caller-actionable error when neither is set. The platform key is removed
outright rather than kept as a fallback, so there is nothing left to silently substitute (the #653
lesson: a silent default is the bug, and #295 before it).

RED until the [impl] lands ``services/model_credential.py`` and rewires the six factories.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("a35472b5-9490-4e22-bf20-3399a5462f5a")
_GRAPH = uuid.UUID("38f43dc4-5797-41b0-964d-a177bb728471")

#: Every KGS factory that builds a model client over customer data (#724, the six sites).
_CUSTOMER_DATA_MODEL_SITES = (
    ("services/entity_extractor.py", "entity extraction reads every ingested chunk"),
    ("services/community_summarizer.py", "community summarisation reads graph content"),
    ("services/vision_extractor.py", "vision extraction reads ingested images"),
    ("services/embedder.py", "the embedder embeds ingested content"),
    ("services/schema_synthesis_service.py", "schema synthesis reads customer content"),
    ("services/code/embeddings.py", "code embeddings embed ingested code"),
)

_KGS_SRC = Path(__file__).resolve().parents[2] / "src" / "oraclous_knowledge_graph_service"


def _resolver():
    """The not-yet-built seam, imported function-locally so collection stays green and the test
    hard-FAILS rather than skipping while it is missing (CLAUDE.md §4.1)."""
    from oraclous_knowledge_graph_service.services.model_credential import (  # noqa: PLC0415
        ModelCredentialUnavailable,
        resolve_model_credential,
    )

    return resolve_model_credential, ModelCredentialUnavailable


class _Broker:
    """A stand-in credential broker: maps credential_id to a resolved payload, org-scoped."""

    def __init__(self, payloads: dict[str, dict[str, str]]) -> None:
        self._payloads = payloads
        self.calls: list[tuple[str, uuid.UUID]] = []

    async def resolve_credential(
        self, *, credential_id: str, organisation_id: uuid.UUID
    ) -> dict[str, str]:
        self.calls.append((credential_id, organisation_id))
        if credential_id not in self._payloads:
            raise KeyError(credential_id)
        return self._payloads[credential_id]


# --- AC1: the platform key is gone, not demoted to a fallback -------------------------------


def test_settings_carries_no_platform_model_key() -> None:
    """#724 AC1. A key that exists is a key something can fall back to. The org's credential is
    the only source, so the field itself goes (and compose stops injecting it)."""
    from oraclous_knowledge_graph_service.core.config import Settings  # noqa: PLC0415

    assert not hasattr(Settings(), "openai_api_key"), (
        "KGS_OPENAI_API_KEY must be removed outright: while it exists, a customer-data model call "
        "can silently run on the platform's key (the #653 / #295 failure mode)"
    )


# --- AC2: resolution order, org default with a per-graph override ---------------------------


@pytest.mark.asyncio
async def test_org_default_credential_is_resolved_when_the_graph_has_no_binding() -> None:
    """#724 AC2. With no per-graph override the org's designated default is used."""
    resolve, _ = _resolver()
    broker = _Broker({"org-default": {"api_key": "sk-org-default"}})

    resolved = await resolve(
        organisation_id=_ORG,
        graph_id=_GRAPH,
        broker=broker,
        org_default_credential_id="org-default",
        graph_credential_id=None,
    )

    assert resolved.api_key == "sk-org-default"
    assert broker.calls == [("org-default", _ORG)]


@pytest.mark.asyncio
async def test_a_graph_binding_overrides_the_org_default() -> None:
    """#724 AC2. A graph may pin its own model, so a sensitive graph is not forced onto the org
    default. The override wins and the default is never resolved."""
    resolve, _ = _resolver()
    broker = _Broker(
        {"org-default": {"api_key": "sk-org-default"}, "graph-pinned": {"api_key": "sk-graph"}}
    )

    resolved = await resolve(
        organisation_id=_ORG,
        graph_id=_GRAPH,
        broker=broker,
        org_default_credential_id="org-default",
        graph_credential_id="graph-pinned",
    )

    assert resolved.api_key == "sk-graph"
    assert [c[0] for c in broker.calls] == ["graph-pinned"], "the org default must not be resolved"


@pytest.mark.asyncio
async def test_the_credential_is_resolved_against_the_calling_org() -> None:
    """ADR-008 / ORG001. The org is server-side context, so the broker call carries it and a
    credential belonging to another org can never be reached."""
    resolve, _ = _resolver()
    broker = _Broker({"org-default": {"api_key": "sk-org-default"}})

    await resolve(
        organisation_id=_ORG,
        graph_id=_GRAPH,
        broker=broker,
        org_default_credential_id="org-default",
        graph_credential_id=None,
    )

    assert broker.calls[0][1] == _ORG


# --- AC3: fail closed, with an error the caller can act on ----------------------------------


@pytest.mark.asyncio
async def test_no_credential_anywhere_fails_closed() -> None:
    """#724 AC3. Neither a graph binding nor an org default configured. The call raises instead
    of running on anything of the platform's."""
    resolve, unavailable = _resolver()
    broker = _Broker({})

    with pytest.raises(unavailable):
        await resolve(
            organisation_id=_ORG,
            graph_id=_GRAPH,
            broker=broker,
            org_default_credential_id=None,
            graph_credential_id=None,
        )

    assert broker.calls == [], "with nothing configured there is nothing to ask the broker for"


@pytest.mark.asyncio
async def test_an_unresolvable_credential_fails_closed_too() -> None:
    """#724 AC3. A configured id the broker cannot resolve (deleted, wrong org) is the same
    outcome as none configured: refuse, never substitute."""
    resolve, unavailable = _resolver()
    broker = _Broker({})

    with pytest.raises(unavailable):
        await resolve(
            organisation_id=_ORG,
            graph_id=_GRAPH,
            broker=broker,
            org_default_credential_id="gone",
            graph_credential_id=None,
        )


@pytest.mark.asyncio
async def test_a_credential_with_no_api_key_fails_closed() -> None:
    """#724 AC3. A resolved payload carrying no usable key is unusable, not empty-string-usable."""
    resolve, unavailable = _resolver()
    broker = _Broker({"org-default": {"connection_string": "postgres://..."}})

    with pytest.raises(unavailable):
        await resolve(
            organisation_id=_ORG,
            graph_id=_GRAPH,
            broker=broker,
            org_default_credential_id="org-default",
            graph_credential_id=None,
        )


@pytest.mark.asyncio
async def test_the_failure_tells_the_caller_what_to_do() -> None:
    """#724 AC3. The operator-facing half. Run `dc167d8e` failed every extraction call and
    reported nothing a user could act on; the reason was only ever in worker logs. The error
    carries a machine-readable code and names the missing configuration."""
    resolve, unavailable = _resolver()
    broker = _Broker({})

    with pytest.raises(unavailable) as excinfo:
        await resolve(
            organisation_id=_ORG,
            graph_id=_GRAPH,
            broker=broker,
            org_default_credential_id=None,
            graph_credential_id=None,
        )

    err = excinfo.value
    assert getattr(err, "error_code", "") == "model_credential_not_configured"
    message = str(err).lower()
    assert "credential" in message
    assert "model" in message
    # It must not leak the key material or the platform's endpoint into a caller-visible error.
    assert "sk-" not in str(err)


# --- AC4: every one of the six factories takes a resolved credential ------------------------


@pytest.mark.parametrize(("relative_path", "why"), _CUSTOMER_DATA_MODEL_SITES)
def test_no_customer_data_model_client_reads_a_platform_key(relative_path: str, why: str) -> None:
    """#724 AC4 + AC5, the enforcement half.

    Static rather than behavioural on purpose: the guarantee is "no site does this", and a
    behavioural test per factory would pass while the seventh site added next month does not
    exist yet. Scans for any attribute read of a settings-borne ``*api_key``.
    """
    source = (_KGS_SRC / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = [
        f"line {node.lineno}: reads {node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.endswith("api_key")
    ]

    assert not offenders, (
        f"{relative_path} builds a model client from platform config, but {why}. "
        f"It must take a broker-resolved org credential (#724). Found: {offenders}"
    )
