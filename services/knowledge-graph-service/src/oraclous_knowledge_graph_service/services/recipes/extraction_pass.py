"""Hybrid free-text-on-a-field extraction pass (ORAA-4 §21 services layer — planning, no driver).

Recipe enrichment Slice 2 (#269). The deterministic recipe engine projects a structured record into
its node/edge graph; THIS pass runs AFTER that projection and mines extra entities from a designated
PROSE field *within* the same record, so a structured record gains the entities its text describes
and records interconnect by the entities they share.

For each `extractions[]` rule in a validated recipe:
  - collect `(primary_node_deterministic_id, text)` for the rule's `from` field across all record
    units (the primary node is the node the rule's `link.from_node_rule` projected for that record;
    its deterministic id is handed off by the engine on the projection result), skipping empty text;
  - build the LLM extractor ONCE from the rule's inline ontology (`to_graph_schema` → hard
    `GraphSchema`, `to_prompt_prefix` → soft steering) and run it over the collected texts (the
    extractor parallelises across them via `max_concurrency`);
  - for each record's extracted entities: write the entity nodes + their inter-relationships through
    the SAME org-scoped `RecipeGraphWriter` the deterministic projection uses — the entities are
    `:Label:__Entity__` nodes stamped with `organisation_id`/`graph_id`/provenance, keyed by the
    SAME `_deterministic_id(graph_id, label, identity_key)` the projection uses, so the same entity
    mentioned in two records MERGE-dedups to one node; the rule's ontology (strict/coerce) is
    enforced on every extracted label; and a `link.type` (e.g. `MENTIONS`) edge is MERGEd from the
    record's primary node to each extracted entity.

Fail-soft (matches the free-text path's `on_error=IGNORE`):
  - `make_extractor` returns None (`KGS_EXTRACTOR=null`) → the WHOLE pass is skipped with a warning;
    the deterministic structured projection is untouched.
  - a per-record extraction/write error is logged + skipped, never sinking the whole ingest.

The extractor's `extract` is async; this pass is called from the synchronous structured-ingest
thread (no running loop), so it drives the extractor via `asyncio.run` — one fresh loop per ingest.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from neo4j_graphrag.experimental.components.types import LexicalGraphConfig, Neo4jGraph

from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.domain.extraction_schema import (
    to_graph_schema,
    to_prompt_prefix,
)
from oraclous_knowledge_graph_service.domain.ontology import Ontology, resolve_label
from oraclous_knowledge_graph_service.domain.structural import StructuralRepresentation
from oraclous_knowledge_graph_service.domain.temporal import (
    TEMPORAL_KEYS,
    normalize_temporal_properties,
    temporal_prompt_steering,
)
from oraclous_knowledge_graph_service.repositories.recipe_write_repository import RecipeGraphWriter
from oraclous_knowledge_graph_service.services.entity_extractor import make_extractor
from oraclous_knowledge_graph_service.services.recipes.resolution_pass import (
    SAME_AS_CANDIDATE,
    ClusterResult,
    ResolutionPlan,
    cluster_canonical_keys,
)

logger = logging.getLogger(__name__)

# The extractor links every entity to its source chunk with this edge type; chunk_id is the record's
# primary node id here, so a FROM_CHUNK edge tells us which RECORD an extracted entity came from. We
# do not write FROM_CHUNK (we emit the recipe's `link.type` MENTIONS edge instead).
_FROM_CHUNK = LexicalGraphConfig().node_to_chunk_relationship_type
# Identity normalization for extracted entities: fold case + whitespace so the same entity NAME
# (regardless of casing/spacing) MERGE-dedups to one node across records.
_NORMALIZE = ("trim", "casefold", "collapse_whitespace")


class _ExtractionStats:
    """Running totals the structured service folds onto the job stats."""

    def __init__(self) -> None:
        self.entities_extracted = 0
        self.mentions = 0
        # Slice 4 resolution: canonical names folded onto a representative by the semantic pass, and
        # ambiguous-band SAME_AS_CANDIDATE edges MERGEd for review.
        self.entities_merged = 0
        self.resolution_candidates = 0
        self.warnings: list[str] = []


def run_extraction_pass(
    *,
    recipe: dict[str, Any],
    representation: StructuralRepresentation,
    writer: RecipeGraphWriter,
    node_index_by_rule: dict[str, dict[str, str]],
    settings: Settings,
    engine: Any,
    meta: dict[str, Any],
    source_id: str,
) -> _ExtractionStats:
    """Run every `extractions[]` rule over the projected records; return the entity/mention totals.

    `node_index_by_rule` is the engine's hand-off: `{node_rule_id: {unit_id: deterministic_id}}`
    from the deterministic projection — used to resolve each record's primary (link source) node id.
    """
    from oraclous_knowledge_graph_service.services.recipes.engine import _deterministic_id

    stats = _ExtractionStats()
    rules = recipe.get("extractions", [])
    if not rules:
        return stats

    record_units = [u for u in representation.units if u.kind.value == "record"]
    graph_id = writer.graph_id

    for rule in rules:
        ontology = Ontology.of(rule["ontology"])
        # #311: a rule may opt into LLM temporal extraction (`temporal: true`) — append the
        # relationship-temporal steering to the ontology prompt prefix so the LLM is asked to mine
        # valid_from/valid_to/event_time/event_time_end onto the relationships it emits.
        extract_temporal = bool(rule.get("temporal", False))
        prompt_prefix = to_prompt_prefix(ontology)
        if extract_temporal:
            steering = temporal_prompt_steering()
            prompt_prefix = f"{prompt_prefix}\n\n{steering}" if prompt_prefix else steering
        extractor = make_extractor(
            settings,
            schema=to_graph_schema(ontology),
            prompt_prefix=prompt_prefix,
        )
        # Fail-soft: extractor off (KGS_EXTRACTOR=null) → skip this rule; the deterministic
        # projection already completed, so the structured graph is unaffected.
        if extractor is None:
            msg = (
                f"extraction rule {rule['id']!r} skipped: the LLM extractor is unavailable "
                f"(KGS_EXTRACTOR=null) — the structured projection is unaffected."
            )
            logger.warning(msg)
            stats.warnings.append(msg)
            continue

        from_ref = rule["from"]
        from_node_rule = rule["link"]["from_node_rule"]
        link_type = rule["link"]["type"]
        primary_by_unit = node_index_by_rule.get(from_node_rule, {})

        # Collect (primary_node_id, text) per record for the `from` field; skip empty text and any
        # record whose primary node was not projected (e.g. ontology-rejected primary label).
        chunk_ids: list[str] = []
        texts: list[str] = []
        for unit in record_units:
            primary_id = primary_by_unit.get(unit.unit_id)
            if primary_id is None:
                continue
            value = engine.read_record_field(unit, from_ref)
            text = "" if value is None else str(value)
            if not text.strip():
                continue
            chunk_ids.append(primary_id)
            texts.append(text)

        if not texts:
            continue

        try:
            extracted = asyncio.run(extractor.extract(chunks=texts, chunk_ids=chunk_ids))
        except Exception:  # noqa: BLE001 — fail-soft: a failed extract never sinks the ingest.
            logger.exception("extraction rule %r: extractor.extract failed; skipping", rule["id"])
            stats.warnings.append(f"extraction rule {rule['id']!r}: extractor failed; skipped.")
            continue

        by_record = _group_by_record(extracted, chunk_ids)
        resolution = rule.get("resolution")
        if resolution:
            # RESOLVE-ON-WRITE (Slice 4): canonicalize the extracted entities, cluster their
            # distinct canonical names per label, then write entities keyed to representatives (one
            # node per cluster, aliases unioned, MENTIONS from all source records) + the
            # ambiguous-band SAME_AS_CANDIDATE edges. Resolving BEFORE write avoids node surgery.
            _resolve_and_write_rule(
                rule=rule,
                by_record=by_record,
                ontology=ontology,
                link_type=link_type,
                graph_id=graph_id,
                writer=writer,
                settings=settings,
                deterministic_id=_deterministic_id,
                meta=meta,
                source_id=source_id,
                stats=stats,
                extract_temporal=extract_temporal,
            )
            continue
        for primary_id, entity_graph in by_record.items():
            try:
                ents, links = _write_record_entities(
                    writer=writer,
                    primary_id=primary_id,
                    entity_graph=entity_graph,
                    ontology=ontology,
                    link_type=link_type,
                    graph_id=graph_id,
                    deterministic_id=_deterministic_id,
                    meta=meta,
                    source_id=source_id,
                    extract_temporal=extract_temporal,
                )
                stats.entities_extracted += ents
                stats.mentions += links
            except Exception:  # noqa: BLE001 — per-record isolation (like free-text on_error=IGNORE).
                logger.exception(
                    "extraction rule %r: writing entities for record %r failed; skipping",
                    rule["id"],
                    primary_id,
                )
                stats.warnings.append(
                    f"extraction rule {rule['id']!r}: a record's entities failed; skipped."
                )
    return stats


def _group_by_record(extracted: Neo4jGraph, chunk_ids: list[str]) -> dict[str, Neo4jGraph]:
    """Split the combined extracted graph back into one sub-graph per record (by primary id).

    The extractor namespaces each entity's id by its chunk id (= the record's primary id) and adds a
    FROM_CHUNK edge entity→chunk_id; that edge is how we recover the record an entity belongs to. A
    relationship between two extracted entities is assigned to the record that owns BOTH endpoints
    (cross-chunk rels, which the schema's patterns make rare, are dropped — they have no single
    owning record). FROM_CHUNK edges are not carried into the per-record sub-graph.
    """
    valid_chunks = set(chunk_ids)
    node_to_chunk: dict[str, str] = {}
    for rel in extracted.relationships:
        if rel.type == _FROM_CHUNK and rel.end_node_id in valid_chunks:
            node_to_chunk[rel.start_node_id] = rel.end_node_id

    by_record: dict[str, Neo4jGraph] = {cid: Neo4jGraph() for cid in valid_chunks}
    for node in extracted.nodes:
        chunk_id = node_to_chunk.get(node.id)
        if chunk_id is not None:
            by_record[chunk_id].nodes.append(node)
    for rel in extracted.relationships:
        if rel.type == _FROM_CHUNK:
            continue
        start_chunk = node_to_chunk.get(rel.start_node_id)
        end_chunk = node_to_chunk.get(rel.end_node_id)
        if start_chunk is not None and start_chunk == end_chunk:
            by_record[start_chunk].relationships.append(rel)
    return {cid: g for cid, g in by_record.items() if g.nodes}


def _write_record_entities(
    *,
    writer: RecipeGraphWriter,
    primary_id: str,
    entity_graph: Neo4jGraph,
    ontology: Ontology,
    link_type: str,
    graph_id: str,
    deterministic_id: Any,
    meta: dict[str, Any],
    source_id: str,
    extract_temporal: bool = False,
) -> tuple[int, int]:
    """Write one record's extracted entities + their inter-rels + the MENTIONS link from its primary
    node. Returns (entities_written, mentions_written). Reuses the deterministic-projection write
    path: every entity is a `:Label:__Entity__` node keyed by `_deterministic_id` (so an identical
    entity in another record MERGE-dedups to one node), org/graph-stamped by the writer, provenance
    INFERRED (the LLM inferred it). Ontology strict/coerce is enforced per the rule's ontology.
    """
    lib_id_to_det: dict[str, str] = {}
    entities = 0
    mentions = 0
    for node in entity_graph.nodes:
        name = (node.properties or {}).get("name")
        if not (isinstance(name, str) and name.strip()):
            continue  # the writer drops empty-name entities on the free-text path too.
        resolved_label, _coerced = resolve_label(ontology, node.label)
        if resolved_label is None:
            continue  # strict/coerce: an off-ontology label is rejected.
        identity_key = _normalize(name)
        if not identity_key:
            continue
        entity_id = deterministic_id(graph_id, resolved_label, identity_key)
        properties = {k: v for k, v in (node.properties or {}).items() if v is not None}
        properties["name"] = name
        writer.merge_node(
            label=resolved_label,
            entity_id=entity_id,
            identity_key=identity_key,
            properties=properties,
            provenance="INFERRED",
            source_id=source_id,
            meta=meta,
            confidence=None,
            container_id=None,
        )
        lib_id_to_det[node.id] = entity_id
        entities += 1
        # MERGE the recipe's link (e.g. MENTIONS) from the record's primary node to this entity. A
        # repeat of the same entity in another record adds another MENTIONS to the one shared node.
        mentions += writer.merge_edge(
            rel_type=link_type,
            edges=[{"from": primary_id, "to": entity_id}],
            source_id=source_id,
            provenance="INFERRED",
            meta=meta,
        )

    # Entity↔entity inter-relationships the extractor found, translated onto the deterministic ids.
    inter_edges_by_type: dict[str, list[dict[str, Any]]] = {}
    for rel in entity_graph.relationships:
        start = lib_id_to_det.get(rel.start_node_id)
        end = lib_id_to_det.get(rel.end_node_id)
        if start is None or end is None:
            continue  # an endpoint was dropped (empty name / off-ontology) — skip dangling edge.
        edge = _edge_with_temporal(start, end, rel, extract_temporal)
        inter_edges_by_type.setdefault(rel.type, []).append(edge)
    for rel_type, edges in inter_edges_by_type.items():
        writer.merge_edge(
            rel_type=rel_type,
            edges=edges,
            source_id=source_id,
            provenance="INFERRED",
            meta=meta,
        )
    return entities, mentions


def _edge_with_temporal(start: str, end: str, rel: Any, extract_temporal: bool) -> dict[str, Any]:
    """Build a `merge_edge` row for one inter-entity relationship, carrying the normalised temporal
    properties (#311) when the rule opts in. Returns the bare `{from, to}` row when temporal
    extraction is off OR the relationship has no temporal field — so a non-temporal rule's edges are
    written exactly as before (no `properties` key). The temporal values are mined by the LLM onto
    the relationship; `normalize_temporal_properties` keeps only the four temporal keys, coerces
    year-only -> full date, and drops blanks (so the edge stores no empty/None temporal property).
    """
    edge: dict[str, Any] = {"from": start, "to": end}
    if not extract_temporal:
        return edge
    raw = rel.properties or {}
    temporal = normalize_temporal_properties({k: raw[k] for k in TEMPORAL_KEYS if k in raw})
    if temporal:
        edge["properties"] = temporal
    return edge


class _ParsedEntity:
    """An extracted entity after label-resolution + canonical keying (one per accepted LLM node)."""

    __slots__ = ("primary_id", "lib_id", "label", "surface_form", "canonical_key", "properties")

    def __init__(
        self,
        *,
        primary_id: str,
        lib_id: str,
        label: str,
        surface_form: str,
        canonical_key: str,
        properties: dict[str, Any],
    ) -> None:
        self.primary_id = primary_id
        self.lib_id = lib_id
        self.label = label
        self.surface_form = surface_form
        self.canonical_key = canonical_key
        self.properties = properties


def _resolve_and_write_rule(
    *,
    rule: dict[str, Any],
    by_record: dict[str, Neo4jGraph],
    ontology: Ontology,
    link_type: str,
    graph_id: str,
    writer: RecipeGraphWriter,
    settings: Settings,
    deterministic_id: Any,
    meta: dict[str, Any],
    source_id: str,
    stats: _ExtractionStats,
    extract_temporal: bool = False,
) -> None:
    """Resolve-on-write for one extraction rule (Slice 4): canonicalize → cluster → write keyed to
    representatives. One node per cluster (aliases unioned, MENTIONS from every source record), the
    ambiguous band MERGEd as SAME_AS_CANDIDATE edges. Folds stats onto `stats`.
    """
    plan = ResolutionPlan.from_rule(rule["resolution"])
    # 1. Parse every record's entities, resolving the label + deriving the canonical key. A bad
    #    label / empty name is dropped exactly as the non-resolution path drops it.
    parsed: list[_ParsedEntity] = []
    for primary_id, entity_graph in by_record.items():
        for node in entity_graph.nodes:
            name = (node.properties or {}).get("name")
            if not (isinstance(name, str) and name.strip()):
                continue
            resolved_label, _coerced = resolve_label(ontology, node.label)
            if resolved_label is None:
                continue
            canonical_key = plan.canonical_key(name)
            if not canonical_key.strip():
                continue
            properties = {k: v for k, v in (node.properties or {}).items() if v is not None}
            parsed.append(
                _ParsedEntity(
                    primary_id=primary_id,
                    lib_id=node.id,
                    label=resolved_label,
                    surface_form=name.strip(),
                    canonical_key=canonical_key,
                    properties=properties,
                )
            )
    if not parsed:
        return

    # 2. Distinct canonical keys per label (+ occurrence counts for the representative tie-break).
    keys_by_label: dict[str, dict[str, int]] = {}
    for ent in parsed:
        counts = keys_by_label.setdefault(ent.label, {})
        counts[ent.canonical_key] = counts.get(ent.canonical_key, 0) + 1

    # 3. Semantic clustering (conservative; fail-soft). Each (label, canonical_key) maps to a
    #    representative canonical key; ambiguous-band pairs come back as candidates.
    cluster: ClusterResult = cluster_canonical_keys(
        keys_by_label=keys_by_label, plan=plan, settings=settings
    )
    stats.warnings.extend(cluster.warnings)
    stats.entities_merged += cluster.merged

    # 4. Resolve each entity to its representative id + gather the cluster's aliases / display form.
    #    aliases = the union of ORIGINAL surface forms across the cluster; canonical_name = the
    #    longest surface form seen (a chosen display form); name = the representative canonical key.
    rep_key_of: dict[tuple[str, str], str] = cluster.representative
    aliases_by_rep: dict[tuple[str, str], list[str]] = {}
    display_by_rep: dict[tuple[str, str], str] = {}
    props_by_rep: dict[tuple[str, str], dict[str, Any]] = {}
    lib_to_rep_id: dict[str, str] = {}
    for ent in parsed:
        rep_key = rep_key_of.get((ent.label, ent.canonical_key), ent.canonical_key)
        rep = (ent.label, rep_key)
        rep_id = deterministic_id(graph_id, ent.label, rep_key)
        lib_to_rep_id[ent.lib_id] = rep_id
        alias_list = aliases_by_rep.setdefault(rep, [])
        if ent.surface_form not in alias_list:
            alias_list.append(ent.surface_form)
        current_display = display_by_rep.get(rep)
        if current_display is None or len(ent.surface_form) > len(current_display):
            display_by_rep[rep] = ent.surface_form
        # Last-writer-wins for non-name properties (the LLM rarely emits extra ones); name/aliases/
        # canonical_name are managed explicitly below.
        merged_props = props_by_rep.setdefault(rep, {})
        for k, v in ent.properties.items():
            if k != "name":
                merged_props[k] = v

    # 5. Write ONE node per representative (keyed to the canonical key) with the alias audit trail,
    #    then MERGE the MENTIONS from every source record's primary node to the representative id.
    written_reps: set[tuple[str, str]] = set()
    mentions_seen: set[tuple[str, str]] = set()
    for ent in parsed:
        rep_key = rep_key_of.get((ent.label, ent.canonical_key), ent.canonical_key)
        rep = (ent.label, rep_key)
        rep_id = deterministic_id(graph_id, ent.label, rep_key)
        if rep not in written_reps:
            properties = dict(props_by_rep.get(rep, {}))
            properties["name"] = rep_key
            properties["canonical_name"] = display_by_rep.get(rep, rep_key)
            try:
                writer.merge_node(
                    label=ent.label,
                    entity_id=rep_id,
                    identity_key=rep_key,
                    properties=properties,
                    provenance="INFERRED",
                    source_id=source_id,
                    meta=meta,
                    confidence=None,
                    container_id=None,
                    aliases=aliases_by_rep.get(rep, []),
                )
                written_reps.add(rep)
                stats.entities_extracted += 1
            except Exception:  # noqa: BLE001 — per-entity isolation (like on_error=IGNORE).
                logger.exception(
                    "extraction rule %r: writing canonical entity %r failed; skipping",
                    rule["id"],
                    rep_key,
                )
                stats.warnings.append(
                    f"extraction rule {rule['id']!r}: a canonical entity failed; skipped."
                )
                continue
        mention_key = (ent.primary_id, rep_id)
        if mention_key in mentions_seen:
            continue  # same record naming two variants of one canonical → one MENTIONS, not two.
        mentions_seen.add(mention_key)
        try:
            stats.mentions += writer.merge_edge(
                rel_type=link_type,
                edges=[{"from": ent.primary_id, "to": rep_id}],
                source_id=source_id,
                provenance="INFERRED",
                meta=meta,
            )
        except Exception:  # noqa: BLE001 — per-record isolation.
            logger.exception(
                "extraction rule %r: writing MENTIONS for record %r failed; skipping",
                rule["id"],
                ent.primary_id,
            )
            stats.warnings.append(
                f"extraction rule {rule['id']!r}: a record's MENTIONS failed; skipped."
            )

    # 6. Inter-entity relationships, re-pointed onto the representative ids (per record sub-graph).
    inter_edges_by_type: dict[str, list[dict[str, Any]]] = {}
    for entity_graph in by_record.values():
        for rel in entity_graph.relationships:
            start = lib_to_rep_id.get(rel.start_node_id)
            end = lib_to_rep_id.get(rel.end_node_id)
            if start is None or end is None or start == end:
                continue  # endpoint dropped, or a within-cluster self-rel after folding.
            inter_edges_by_type.setdefault(rel.type, []).append(
                _edge_with_temporal(start, end, rel, extract_temporal)
            )
    for rel_type, edges in inter_edges_by_type.items():
        writer.merge_edge(
            rel_type=rel_type, edges=edges, source_id=source_id, provenance="INFERRED", meta=meta
        )

    # 7. Ambiguous-band SAME_AS_CANDIDATE edges: MERGE one between the two canonical nodes (NOT a
    #    merge) carrying the cosine `score`, for human review. Both endpoints were written above (a
    #    candidate key is its own representative — it was not folded).
    candidate_edges: list[dict[str, Any]] = []
    for label, key_a, key_b, score in cluster.candidates:
        id_a = deterministic_id(graph_id, label, key_a)
        id_b = deterministic_id(graph_id, label, key_b)
        if id_a == id_b:
            continue
        candidate_edges.append({"from": id_a, "to": id_b, "properties": {"score": score}})
    if candidate_edges:
        # Suppression-aware MERGE: a pair a human already REJECTED (#279) carries a NOT_SAME_AS
        # edge and is not re-flagged. `merge_candidate_edges` honours that; fall back to the generic
        # `merge_edge` for any writer that predates it (e.g. a narrow test double).
        merge_candidates = getattr(writer, "merge_candidate_edges", None)
        if merge_candidates is not None:
            stats.resolution_candidates += merge_candidates(
                edges=candidate_edges,
                source_id=source_id,
                provenance="INFERRED",
                meta=meta,
            )
        else:
            stats.resolution_candidates += writer.merge_edge(
                rel_type=SAME_AS_CANDIDATE,
                edges=candidate_edges,
                source_id=source_id,
                provenance="INFERRED",
                meta=meta,
            )


def _normalize(value: str) -> str:
    text = value
    for op in _NORMALIZE:
        if op == "trim":
            text = text.strip()
        elif op == "casefold":
            text = text.casefold()
        elif op == "collapse_whitespace":
            text = " ".join(text.split())
    return text
