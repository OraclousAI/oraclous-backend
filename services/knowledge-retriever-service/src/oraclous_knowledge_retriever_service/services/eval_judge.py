"""KRS's BYOM/settings wiring around the shared LLM-as-judge seam (#331/#333, ADR-037).

The ``EvalJudge`` protocol + the real ``OpenAIEvalJudge`` are the SHARED evaluation seam, imported
from ``oraclous_eval`` (the canonical home, #597) and re-exported here — so existing KRS imports
keep working AND "the KRS judge" is enforced-by-code to be the SAME class the compiler eval-set uses
(the two byte-identical copies are now one). This module owns only the KRS-specific construction:
``make_judge`` (operator key → judge, built ONCE at lifespan as ``app.state.eval_judge``, closed on
shutdown via :meth:`OpenAIEvalJudge.aclose`) and ``resolve_byom_judge`` (a per-request judge from a
broker-resolved BYOM credential, ADR-037 BYOM-judge). The timeout posture (#333: a short per-call
timeout + bounded retries vs the SDK's 600s×3) lives in ``Settings``, threaded in here.

No key configured → :func:`make_judge` returns None and the DI layer maps that to a typed 422 — an
explicit evaluation endpoint must refuse rather than silently fabricate scores.
"""

from __future__ import annotations

import uuid

from oraclous_eval import EvalJudge, OpenAIEvalJudge

from oraclous_knowledge_retriever_service.core.config import Settings
from oraclous_knowledge_retriever_service.services.broker_client import BrokerClient, BrokerError

# EvalJudge / OpenAIEvalJudge are re-exported from oraclous_eval (the canonical seam) so KRS callers
# keep importing them from here.
__all__ = ["EvalJudge", "OpenAIEvalJudge", "make_judge", "resolve_byom_judge"]


def _model_from_binding(binding: str | None) -> str | None:
    """A model binding ``<provider>/<model-id>`` → the provider's model id (split on the FIRST '/',
    mirroring the harness): ``openrouter/openai/gpt-4o-mini`` → ``openai/gpt-4o-mini`` (what
    OpenRouter wants). KRS owns this single split so the engine never pre-splits."""
    if not binding:
        return None
    return binding.split("/", 1)[1] if "/" in binding else binding


async def resolve_byom_judge(
    settings: Settings,
    *,
    credential_id: str,
    judge_model: str | None,
    organisation_id: uuid.UUID,
) -> OpenAIEvalJudge:
    """Build a PER-REQUEST judge from a broker-resolved BYOM credential (ADR-037 / BYOM-judge).

    The user's OpenRouter key never lives in KRS config — it was stored via the gateway credentials
    API and KRS resolves it per-org from the credential-broker (``X-Internal-Key``, org-scoped;
    ADR-008 operator separation), then builds an :class:`OpenAIEvalJudge` for THIS request only (the
    caller must ``aclose()`` it). The base_url is the operator OpenRouter default — a user-supplied
    custom base_url is intentionally NOT honoured here (no egress guard needed). Raises
    :class:`BrokerError` when the credential is missing/unresolvable → the route fails it closed.
    """
    broker = BrokerClient(
        settings.credential_broker_url or "", internal_key=settings.internal_service_key or ""
    )
    try:
        payload = await broker.resolve_credential(
            credential_id=credential_id, organisation_id=organisation_id
        )
    finally:
        await broker.aclose()
    api_key = payload.get("api_key") or payload.get("key")
    if not api_key:
        raise BrokerError("BYOM judge credential has no api_key")
    return OpenAIEvalJudge(
        api_key=str(api_key),
        base_url=settings.openai_base_url,  # operator default (OpenRouter); user base_url ignored
        model_name=_model_from_binding(judge_model) or settings.eval_judge_model,
        timeout_seconds=settings.eval_judge_timeout_seconds,
        max_retries=settings.eval_judge_max_retries,
        max_completion_tokens=settings.eval_judge_max_tokens,
    )


def make_judge(settings: Settings) -> OpenAIEvalJudge | None:
    """Build the judge from config, or None when no API key is configured.

    Called ONCE at lifespan; the caller (the DI provider, off ``app.state``) maps None to a typed
    422: scores from an unconfigured judge would be fabrications, and an explicit eval endpoint
    must never return those (#331).
    """
    if not settings.openai_api_key:
        return None
    return OpenAIEvalJudge(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model_name=settings.eval_judge_model,
        timeout_seconds=settings.eval_judge_timeout_seconds,
        max_retries=settings.eval_judge_max_retries,
        max_completion_tokens=settings.eval_judge_max_tokens,
    )
