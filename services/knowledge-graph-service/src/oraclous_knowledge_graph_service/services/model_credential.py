"""Org BYOM model-credential resolution (services layer) — #724 / ADR-008 §3.6.

Every model call KGS makes reads customer data: entity extraction reads each chunk of each
ingested document, and the summariser, vision extractor, embedder, schema synthesiser and code
embedder read the rest. Those clients used to be built from ``KGS_OPENAI_API_KEY``, the platform's
own key, so the customer neither chose the model that read their data nor saw what it cost.

This is the single seam that answers "whose key". It mirrors the knowledge-retriever's
``resolve_byom_judge`` (ADR-037), the pattern already proven in this codebase: broker-resolved,
org-scoped, per-call, fail-closed.

Resolution order, decided on #724::

    graph.model_credential_id  ->  the org's default  ->  ModelCredentialUnavailable

The two ids arrive as arguments rather than being looked up here, which keeps this module free of
any opinion about where they are stored (a broker-side default marker and a column on the graph)
and free of I/O beyond the injected broker port.

There is deliberately NO platform-key fallback. The key was removed from the deployment rather than
demoted, so a missing credential is a refusal instead of a silent substitution. #653 and #295 each
removed exactly that substitution once already; keeping one here would rebuild it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol


class ModelCredentialUnavailable(Exception):
    """No usable org model credential, so no model call may be made.

    The message leads with ``error_code`` on purpose. Ingestion runs in a Celery worker after the
    request has returned, and ``tasks/ingest_tasks.py`` records a failure as
    ``error_message=str(exc)`` on the job. That string is the whole of what a user ever sees, so a
    code held only on an attribute would never reach them.
    """

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code


@dataclass(frozen=True)
class ModelCredential:
    """A resolved BYOM model credential. Carries the key only for the call being built."""

    api_key: str
    credential_id: str


class ModelCredentialBrokerPort(Protocol):
    """The slice of the credential-broker this seam needs (so a fake satisfies it in tests)."""

    async def resolve_credential(
        self, *, credential_id: str, organisation_id: uuid.UUID
    ) -> dict[str, Any]: ...


class DefaultLookupBrokerPort(ModelCredentialBrokerPort, Protocol):
    """A broker that can also answer which credential the org designated as its default."""

    async def org_default_credential_id(
        self, *, organisation_id: uuid.UUID, purpose: str = "model"
    ) -> str | None: ...


def _api_key_of(payload: dict[str, Any]) -> str | None:
    """The usable key out of a resolved payload, or None. Mirrors the retriever's BYOM judge,
    which accepts either shape the broker stores."""
    for field in ("api_key", "key"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


async def resolve_model_credential(
    *,
    organisation_id: uuid.UUID,
    graph_id: uuid.UUID | None,
    broker: ModelCredentialBrokerPort,
    org_default_credential_id: str | None,
    graph_credential_id: str | None,
) -> ModelCredential:
    """Resolve the credential a model call over this org's data must use.

    Fail-closed on every weaker shape: neither id configured, an id the broker cannot resolve
    (deleted, or belonging to another org), and a payload carrying no usable key. The org is
    server-side context and is passed to the broker on every call, so a credential outside the
    caller's organisation is unreachable (ADR-006 / ORG001).
    """
    credential_id = graph_credential_id or org_default_credential_id
    if not credential_id:
        raise ModelCredentialUnavailable(
            "no model credential is configured for this organisation. Store one through the "
            "credentials API and designate it as the organisation default, or bind one to this "
            f"graph ({graph_id}).",
            error_code="model_credential_not_configured",
        )

    try:
        payload = await broker.resolve_credential(
            credential_id=credential_id, organisation_id=organisation_id
        )
    except Exception as exc:  # noqa: BLE001 — any broker failure is the same refusal
        # The broker's own message may carry transport detail; the id is safe to name, the
        # payload never is.
        raise ModelCredentialUnavailable(
            f"the configured model credential {credential_id} could not be resolved for this "
            f"organisation: {type(exc).__name__}",
            error_code="model_credential_unresolvable",
        ) from exc

    api_key = _api_key_of(payload)
    if api_key is None:
        raise ModelCredentialUnavailable(
            f"the configured model credential {credential_id} holds no usable api_key",
            error_code="model_credential_unusable",
        )
    return ModelCredential(api_key=api_key, credential_id=credential_id)


async def resolve_for_graph(
    *,
    organisation_id: uuid.UUID,
    graph_id: uuid.UUID | None,
    graph_credential_id: str | None,
    broker: DefaultLookupBrokerPort,
) -> ModelCredential:
    """The call-site convenience: look the org default up, then apply the #724 resolution order.

    Separated from :func:`resolve_model_credential` so that function stays pure of the lookup and
    every test can drive the order without a broker that knows about defaults. Callers that already
    hold both ids should use the lower-level function directly.
    """
    org_default = await broker.org_default_credential_id(
        organisation_id=organisation_id, purpose="model"
    )
    return await resolve_model_credential(
        organisation_id=organisation_id,
        graph_id=graph_id,
        broker=broker,
        org_default_credential_id=org_default,
        graph_credential_id=graph_credential_id,
    )


def model_call_is_configured(settings: Any) -> bool:
    """True when this deployment will actually call a model, so a credential must be resolved.

    The key-free modes (``KGS_EXTRACTOR=null`` with a non-openai embedder) call nothing, so a
    caller on those skips resolution entirely: no broker call, no credential, behaviour identical
    to before #724. This is what keeps CI and a key-free deployment untouched.
    """
    return settings.extractor == "openai" or settings.embedder == "openai"


async def credential_for_graph(
    settings: Any,
    *,
    organisation_id: uuid.UUID,
    graph_id: uuid.UUID | None,
    graph_credential_id: str | None,
    broker_factory: Any,
) -> ModelCredential | None:
    """The org credential for work on ``graph_id``, or None when no model will be called.

    The one entry point every KGS call site uses, so the skip rule and the broker lifecycle live in
    one place rather than being re-derived at ten sites. ``broker_factory`` is
    ``credential_client.make_credential_broker``, injected to keep this module free of config
    imports and easy to drive in tests.
    """
    if not model_call_is_configured(settings):
        return None
    broker = broker_factory(settings)
    try:
        return await resolve_for_graph(
            organisation_id=organisation_id,
            graph_id=graph_id,
            graph_credential_id=graph_credential_id,
            broker=broker,
        )
    finally:
        await broker.aclose()
