"""LLM judge client for retrieval-quality evaluation (ORAA-4 §21 services layer) (#331).

KRS's ONE OpenAI-compatible client. Mirrors how KGS builds its extractor/summarizer client
(``KGS_OPENAI_*`` → OpenRouter by default): the judge is an injectable protocol so unit tests pass
a fake with no network, and the real implementation lazily imports ``openai`` so the key-free path
(CI default) never pulls the dependency at import time.

Timeout posture (#333): the client is constructed with an explicit short per-call timeout and
bounded retries (``KRS_EVAL_JUDGE_TIMEOUT_SECONDS`` / ``KRS_EVAL_JUDGE_MAX_RETRIES``) — the SDK
defaults (600s × 3 attempts) would burn far past the gateway's 30s read timeout. The judge is
built ONCE at lifespan (``app.state.eval_judge``, like the Neo4j/Redis clients), never per
request, and closed on shutdown via :meth:`OpenAIEvalJudge.aclose`.

No key configured → :func:`make_judge` returns None and the DI layer maps that to a typed 422 — an
explicit evaluation endpoint must refuse rather than silently fabricate scores.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from oraclous_knowledge_retriever_service.core.config import Settings


@runtime_checkable
class EvalJudge(Protocol):
    """The minimal LLM seam evaluation needs: JSON-object judging + free-text generation."""

    async def complete_json(self, *, system: str, user: str) -> str:
        """Return the model's response text (expected to be a JSON object string)."""
        ...

    async def complete_text(self, *, system: str, user: str) -> str:
        """Return the model's free-text response (used for grounded answer generation)."""
        ...


class OpenAIEvalJudge:
    """The real :class:`EvalJudge` — OpenAI-compatible chat completions (OpenRouter default).

    Mirrors KGS's ``OpenAICommunityLLM``: temperature 0 for a deterministic judge, and a
    JSON-object response format on judging calls so the parse is reliable across providers.
    ``max_completion_tokens`` bounds the JSON judging calls — sized for decomposition output
    (a claims/statements list), which 600 tokens visibly truncated on long answers (#333).
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model_name: str,
        timeout_seconds: float = 15.0,
        max_retries: int = 1,
        max_completion_tokens: int = 2000,
    ) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
        self._model = model_name
        self._max_completion_tokens = max_completion_tokens

    async def complete_json(self, *, system: str, user: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=self._max_completion_tokens,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or "{}"

    async def complete_text(self, *, system: str, user: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=800,
        )
        return response.choices[0].message.content or ""

    async def aclose(self) -> None:
        """Close the underlying HTTP client (lifespan shutdown)."""
        await self._client.close()


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
