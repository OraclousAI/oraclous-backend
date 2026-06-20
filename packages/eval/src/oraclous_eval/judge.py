"""The one LLM-as-judge seam (ADR-037 Decision 1 — promoted verbatim from KRS #331/#333).

An injectable ``EvalJudge`` protocol (unit tests pass a fake, no network) + the real
``OpenAIEvalJudge`` (OpenAI-compatible, OpenRouter default, temperature 0, JSON-object response,
explicit short timeout + bounded retries — the SDK 600s×3 would burn past a 30s read deadline).
Built ONCE from a ``JudgeConfig``; ``make_judge`` returns ``None`` when no key is configured, and
the caller maps that to a typed 422 (an explicit evaluation must refuse, never fabricate scores).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class EvalJudge(Protocol):
    """The minimal LLM seam evaluation needs: JSON-object judging + free-text generation."""

    async def complete_json(self, *, system: str, user: str) -> str:
        """Return the model's response text (expected to be a JSON object string)."""
        ...

    async def complete_text(self, *, system: str, user: str) -> str:
        """Return the model's free-text response."""
        ...


@dataclass(frozen=True)
class JudgeConfig:
    """Service-agnostic judge config (each consuming service maps its own settings to this)."""

    api_key: str | None
    base_url: str = "https://openrouter.ai/api/v1"
    model_name: str = "openai/gpt-4o-mini"
    timeout_seconds: float = 15.0
    max_retries: int = 1
    max_completion_tokens: int = 2000


class OpenAIEvalJudge:
    """The real :class:`EvalJudge` — OpenAI-compatible chat completions. Temperature 0 for a
    deterministic judge; a JSON-object response format on judging calls so the parse is reliable
    across providers. Lazily imports ``openai`` so the key-free path never pulls the dependency."""

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
            api_key=api_key, base_url=base_url, timeout=timeout_seconds, max_retries=max_retries
        )
        self._model = model_name
        self._max_completion_tokens = max_completion_tokens

    async def complete_json(self, *, system: str, user: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=self._max_completion_tokens,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or "{}"

    async def complete_text(self, *, system: str, user: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=800,
        )
        return response.choices[0].message.content or ""

    async def aclose(self) -> None:
        await self._client.close()


def make_judge(config: JudgeConfig) -> OpenAIEvalJudge | None:
    """Build the judge, or ``None`` when no API key is configured (caller → typed 422)."""
    if not config.api_key:
        return None
    return OpenAIEvalJudge(
        api_key=config.api_key,
        base_url=config.base_url,
        model_name=config.model_name,
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        max_completion_tokens=config.max_completion_tokens,
    )
