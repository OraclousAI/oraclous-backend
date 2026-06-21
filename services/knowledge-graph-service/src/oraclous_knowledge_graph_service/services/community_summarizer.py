"""Community LLM summarisation (services layer) (#303).

Restores the legacy ``community_summarizer.py``: for each detected community, an LLM reads
its member entities + the relationships between them and produces a ``summary`` (2-3 sentences), a
``summary_keywords`` list (the key entities/themes), and a ``summary_excerpt`` (the single most
representative line), plus provenance (``summary_model`` + ``summary_at``). RE-ARCHITECTED for the
new build: the member/relationship reads come from the in-DB :class:`CommunityRepository` (so this
service holds NO Cypher and NO Neo4j driver — STR004), and the summary is persisted back through the
same repository.

LLM seam: the SAME OpenAI-compatible client KGS already uses for extraction
(``KGS_EXTRACTOR=openai`` → OpenRouter via ``KGS_OPENAI_*``). The client is an injectable
``CommunityLLM`` protocol, so unit tests pass a fake that returns a fixed JSON with no network.
Concurrency is bounded by an ``asyncio.Semaphore`` (mirrors the #272 extractor concurrency fix) so a
graph with hundreds of communities does not fan out into hundreds of simultaneous round-trips.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol, runtime_checkable

from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.domain.community import CommunityMember
from oraclous_knowledge_graph_service.repositories.community_repository import CommunityRepository

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a knowledge-graph analyst. You summarise a cluster of related entities from a "
    "knowledge graph. Respond ONLY with a JSON object."
)

_USER_TEMPLATE = """\
Here is a community of {entity_count} related entities from a knowledge graph.

Entities:
{entity_list}

Key relationships between members:
{relationship_list}

Return a JSON object with exactly these keys:
- "summary": a 2-3 sentence plain-text description of what these entities have in common (the theme
  or domain), naming the most central entity if one stands out. No bullet points.
- "keywords": a JSON array of 5-10 short strings — the key entities or themes.
- "excerpt": a single short line (<= 200 chars) capturing the single most representative fact.
"""

# Member / relationship sampling caps for the prompt (bound prompt size; legacy used 20 / 10).
_MEMBER_SAMPLE = 20
_REL_SAMPLE = 10
_EXCERPT_MAX = 500


@runtime_checkable
class CommunityLLM(Protocol):
    """The minimal LLM seam the summarizer needs: one JSON-returning chat completion.

    The real implementation wraps the OpenAI-compatible client (OpenRouter); tests inject a fake.
    """

    async def complete_json(self, *, system: str, user: str) -> str:
        """Return the model's response text (expected to be a JSON object string)."""
        ...


class SummaryResult:
    """One community's summary fields (plain value object set after persistence).

    ``source`` is ``"llm"`` for a real model answer or ``"fallback"`` for the member-derived
    degrade — the provenance a reader uses to tell a real summary from a placeholder.
    """

    def __init__(
        self,
        *,
        community_id: str,
        summary: str,
        keywords: list[str],
        excerpt: str,
        source: str,
    ) -> None:
        self.community_id = community_id
        self.summary = summary
        self.keywords = keywords
        self.excerpt = excerpt
        self.source = source


class SummarizeOutcome:
    """The result of a graph summarise pass — DISTINGUISHABLE so the caller can tell a completed run
    from one that was capped/deferred.

    ``status`` is ``"completed"`` when the candidates were summarised inline, or ``"deferred"`` when
    the candidate count exceeded the inline cap and none ran (the caller should use the async detect
    path, which summarises on the worker). ``deferred_count`` is the candidate count that was
    skipped on a deferral (0 on a completed run).
    """

    def __init__(
        self, *, results: list[SummaryResult], status: str, deferred_count: int = 0
    ) -> None:
        self.results = results
        self.status = status
        self.deferred_count = deferred_count


class CommunitySummarizer:
    """Summarise a graph's communities with a bounded-concurrency LLM fan-out."""

    def __init__(
        self,
        *,
        repo: CommunityRepository,
        llm: CommunityLLM,
        model_name: str,
        max_concurrency: int = 5,
    ) -> None:
        self._repo = repo
        self._llm = llm
        self._model = model_name
        self._max_concurrency = max(1, max_concurrency)

    async def summarize_graph(
        self,
        *,
        graph_id: str,
        level: int | None = None,
        force: bool = False,
        max_communities: int | None = None,
    ) -> SummarizeOutcome:
        """Summarise the graph's communities. Returns a DISTINGUISHABLE :class:`SummarizeOutcome`.

        By default (``force`` False) only communities with no real summary yet are summarised — so
        a re-run resumes after a partial failure and never re-bills a community already done. When
        ``max_communities`` is set and there are MORE candidates than that, the batch is too large
        to run inline: the outcome is ``status="deferred"`` with the candidate count (none ran), so
        the caller can tell a deferral from "nothing to do" and route large summarise to the async
        path. Bounded concurrency; one bad community never sinks the rest.
        """
        communities = await asyncio.to_thread(
            self._repo.list_communities,
            graph_id=graph_id,
            level=level,
            min_entities=1,
            only_unsummarized=not force,
        )
        if not communities:
            return SummarizeOutcome(results=[], status="completed")
        if max_communities is not None and len(communities) > max_communities:
            logger.info(
                "community summarize for %s deferred: %d candidates exceed the inline cap %d",
                graph_id,
                len(communities),
                max_communities,
            )
            return SummarizeOutcome(results=[], status="deferred", deferred_count=len(communities))
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def _one(community_id: str) -> SummaryResult | None:
            async with semaphore:
                return await self._summarize_one(graph_id=graph_id, community_id=community_id)

        outcomes = await asyncio.gather(
            *(_one(c.community_id) for c in communities), return_exceptions=True
        )
        results: list[SummaryResult] = []
        for community, outcome in zip(communities, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.warning(
                    "community summary failed for %s: %r", community.community_id, outcome
                )
                continue
            if outcome is not None:
                results.append(outcome)
        return SummarizeOutcome(results=results, status="completed")

    async def _summarize_one(self, *, graph_id: str, community_id: str) -> SummaryResult | None:
        members, rels = await asyncio.to_thread(
            self._repo.members_with_relationships,
            graph_id=graph_id,
            community_id=community_id,
            member_limit=_MEMBER_SAMPLE,
            rel_limit=_REL_SAMPLE,
        )
        if not members:
            return None
        prompt = _USER_TEMPLATE.format(
            entity_count=len(members),
            entity_list=_format_members(members),
            relationship_list=_format_relationships(rels),
        )
        raw = await self._llm.complete_json(system=_SYSTEM_PROMPT, user=prompt)
        summary, keywords, excerpt, source = _parse_summary(raw, fallback_members=members)
        await asyncio.to_thread(
            self._repo.set_summary,
            graph_id=graph_id,
            community_id=community_id,
            summary=summary,
            summary_keywords=keywords,
            summary_excerpt=excerpt,
            # Provenance honesty: a member-derived fallback NEVER reached the model, so it carries
            # no model name (None) and source='fallback' — readers can tell it from a real summary.
            summary_model=self._model if source == "llm" else None,
            summary_source=source,
        )
        return SummaryResult(
            community_id=community_id,
            summary=summary,
            keywords=keywords,
            excerpt=excerpt,
            source=source,
        )


def _format_members(members: list[CommunityMember]) -> str:
    return "\n".join(f"- {m.entity_name or m.entity_id} ({m.entity_type})" for m in members)


def _format_relationships(rels: list[dict[str, str]]) -> str:
    if not rels:
        return "(no direct relationships found)"
    return "\n".join(f"- {r['src']} --[{r['rel']}]--> {r['tgt']}" for r in rels)


def _parse_summary(
    raw: str, *, fallback_members: list[CommunityMember]
) -> tuple[str, list[str], str, str]:
    """Parse the model's JSON into (summary, keywords, excerpt, source). A malformed/empty response
    degrades to a deterministic member-derived fallback — never raises (one bad community must not
    sink the batch, and the community still gets a usable summary). The fourth element is the
    provenance: ``"llm"`` when the model produced a non-empty summary, ``"fallback"`` otherwise — so
    the persisted summary can be honestly stamped (a fallback carries no model name)."""
    try:
        data = json.loads(raw)
        summary = str(data.get("summary") or "").strip()
        keywords_raw = data.get("keywords") or []
        keywords = [str(k).strip() for k in keywords_raw if str(k).strip()][:10]
        excerpt = str(data.get("excerpt") or "").strip()[:_EXCERPT_MAX]
    except (json.JSONDecodeError, TypeError, AttributeError):
        summary, keywords, excerpt = "", [], ""
    # The summary is the load-bearing field: a usable one from the model means a real summary.
    source = "llm" if summary else "fallback"
    if not summary:
        names = [m.entity_name or m.entity_id for m in fallback_members[:5]]
        summary = f"Community of {len(fallback_members)} entities including: {', '.join(names)}"
    if not keywords:
        keywords = [m.entity_name or m.entity_id for m in fallback_members[:5]]
    if not excerpt:
        excerpt = summary[:_EXCERPT_MAX]
    return summary, keywords, excerpt, source


class OpenAICommunityLLM:
    """The real :class:`CommunityLLM` — an OpenAI-compatible chat completion (OpenRouter default).

    Mirrors ``entity_extractor.make_extractor``: lazily imports ``openai`` so the key-free path
    never pulls the dependency, points the client at ``KGS_OPENAI_BASE_URL`` with the API key,
    and requests a JSON object so the parse is reliable across providers.
    """

    def __init__(self, *, api_key: str, base_url: str, model_name: str) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model_name

    async def complete_json(self, *, system: str, user: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or "{}"


def make_summarizer(settings: Settings, *, repo: CommunityRepository) -> CommunitySummarizer | None:
    """Build the summarizer from config, or None when LLM summarisation is off.

    Reuses the extractor's LLM seam: enabled when ``KGS_EXTRACTOR=openai`` (the same OpenAI-compat
    key). Fail-closed: ``openai`` mode with no key configured raises rather than silently producing
    fallback-only summaries that look real but never reached a model.
    """
    if settings.extractor != "openai":
        return None
    if not settings.openai_api_key:
        raise RuntimeError(
            "community summarisation (KGS_EXTRACTOR=openai) requires KGS_OPENAI_API_KEY"
        )
    llm = OpenAICommunityLLM(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model_name=settings.extractor_model,
    )
    return CommunitySummarizer(
        repo=repo,
        llm=llm,
        model_name=settings.extractor_model,
        max_concurrency=settings.extractor_max_concurrency,
    )
