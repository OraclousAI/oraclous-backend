"""Schema-synthesis use-case (services layer) — an authoring aid (Slice C).

Infer a suggested typed ontology from a text SAMPLE, so an author does not have to hand-write the
entity/relationship types before they ingest. It wraps neo4j-graphrag's
``SchemaFromTextExtractor`` (the same library the free-text extractor uses) — the LLM reads the
sample and returns a native ``GraphSchema``, which is projected (in ``domain.extraction_schema``)
into the SAME ``{mode, entity_types, relationship_types}`` shape the ontology PUT accepts, so a
suggestion saves verbatim.

The LLM is an injectable ``LLMInterface`` (tests pass a fake ``SchemaFromTextExtractor`` factory or
a stub LLM, so no network is required). ``make_synthesizer`` builds the real one from config — and
fails closed when no LLM is configured (``KGS_EXTRACTOR=null``), since a synthesis without an LLM
would be a silent no-op.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from neo4j_graphrag.experimental.components.schema import (
    GraphSchema,
    SchemaFromTextExtractor,
)
from neo4j_graphrag.llm import LLMInterface

from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.domain.extraction_schema import from_graph_schema

# A factory the service calls to run the inference: (text) -> GraphSchema (or an awaitable of one,
# which is what SchemaFromTextExtractor.run returns). Injected so tests can supply a deterministic
# schema without an LLM; the default wraps SchemaFromTextExtractor.run.
InferFn = Callable[[str], "GraphSchema | Awaitable[GraphSchema]"]


class SchemaSynthesisUnavailable(Exception):
    """No LLM is configured for schema synthesis (KGS_EXTRACTOR=null). Maps to 503."""


class SchemaSynthesisService:
    """Suggest a typed ontology from a text sample via an injectable inference fn."""

    def __init__(self, infer: InferFn, *, default_mode: str = "strict") -> None:
        self._infer = infer
        self._default_mode = default_mode

    async def suggest(self, *, sample: str, mode: str | None = None) -> dict:
        """Return an Ontology-shaped suggestion ``{mode, entity_types, relationship_types}``.

        The LLM-inferred ``GraphSchema`` is projected to the ontology dict the existing PUT accepts.
        """
        schema = await self._run_infer(sample)
        return from_graph_schema(schema, mode=mode or self._default_mode)

    async def _run_infer(self, sample: str) -> GraphSchema:
        result = self._infer(sample)
        # SchemaFromTextExtractor.run is async; a test fake may return the schema directly. Accept
        # both: await a coroutine, pass a ready GraphSchema through.
        if isinstance(result, GraphSchema):
            return result
        return await result


def make_synthesizer(settings: Settings, *, default_mode: str = "strict") -> SchemaSynthesisService:
    """Build the real synthesizer from config; fail closed when no LLM is configured.

    Mirrors ``entity_extractor.make_extractor``: ``KGS_EXTRACTOR=openai`` + a key builds an
    OpenAI-compatible LLM (OpenRouter base by default) and wires it into
    ``SchemaFromTextExtractor``. ``KGS_EXTRACTOR=null`` (or a missing key) raises — synthesis has no
    deterministic fallback, so it never silently returns an empty ontology.
    """
    if settings.extractor == "null":
        raise SchemaSynthesisUnavailable(
            "schema synthesis requires an LLM (set KGS_EXTRACTOR=openai)"
        )
    if not settings.openai_api_key:
        raise SchemaSynthesisUnavailable("KGS_EXTRACTOR=openai requires KGS_OPENAI_API_KEY")
    # Lazy import so the key-free `null` path never imports openai.
    from neo4j_graphrag.llm import OpenAILLM

    llm: LLMInterface = OpenAILLM(
        model_name=settings.extractor_model,
        model_params={"temperature": 0.0, "response_format": {"type": "json_object"}},
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    extractor = SchemaFromTextExtractor(llm=llm)
    return SchemaSynthesisService(lambda text: extractor.run(text=text), default_mode=default_mode)
