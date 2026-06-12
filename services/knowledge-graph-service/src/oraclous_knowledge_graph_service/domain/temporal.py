"""LLM temporal extraction (ORAA-4 §21 domain layer — pure, no I/O).

Restores the legacy `pipeline_service.py` temporal capability (#311), lift-and-reshaped onto the
shipped #269 recipe extraction pass instead of a parallel pipeline. The recipe model already CARRIES
the temporal fields (`valid_from`/`valid_to`/`event_time` are stamped on structured projections);
what was dropped in the R3.5 simplification is the LLM mining of those fields FROM PROSE on the
extracted relationships. This module is the pure half of that restoration — two functions, no I/O:

  - `temporal_prompt_steering()` -> str
      The relationship-temporal extraction rules + examples, lifted from the legacy
      `RELATIONSHIP_PROPERTY_PROMPT_TEMPLATE`'s TEMPORAL/EVENT-TIME sections and reshaped into a
      prompt-PREFIX block. Appended (when a rule opts in via `temporal: true`) to the ontology's
      soft-steering prefix that already flows into the extractor's `extract_for_chunk`, so the LLM
      is asked to put `valid_from`/`valid_to`/`event_time`/`event_time_end` on the RELATIONSHIP.

  - `normalize_temporal_properties(props)` -> dict
      The legacy post-extraction normalization, reshaped to be pure: coerce a year-only string
      (`"2023"`) to a full ISO-8601 date (`"2023-01-01"`), and DROP a falsy/blank value so the edge
      stores no key (rather than a `None`/empty-string property) where the field is absent. Applied
      to the temporal keys on every extracted inter-entity relationship before it is written. The
      output is only ever a property VALUE — never interpolated into Cypher — so this is safe.

Reshape vs. legacy: legacy mutated each `rel.properties` in place AND applied a job-level
`temporal_context` override (a request-level world-time default). The recipe path has no such
job-level override surface, so only the per-relationship mining + normalization is restored here;
the SAME four field names are used, so a temporal read/query layer over the written edges is
unchanged.
"""

from __future__ import annotations

import re
from typing import Any

# The four temporal property keys mined onto an extracted relationship (legacy `pipeline_service`):
# `valid_from`/`valid_to` are the BELIEF-time bounds (when the relationship became / stopped being
# true); `event_time`/`event_time_end` are the real-world EVENT bounds. Both pairs live on the
# RELATIONSHIP, never on entity nodes.
TEMPORAL_KEYS: tuple[str, ...] = ("valid_from", "valid_to", "event_time", "event_time_end")

_YEAR_ONLY = re.compile(r"\d{4}")

# The temporal steering prefix, lifted from the legacy RELATIONSHIP_PROPERTY_PROMPT_TEMPLATE's
# TEMPORAL/EVENT-TIME sections + examples and reshaped into a standalone prompt-prefix block. No
# `{schema}`/`{examples}` slots — this is appended to the ontology prefix the extractor already
# formats into its prompt, so it must be plain literal text.
_TEMPORAL_STEERING = """\
## Temporal Extraction

Put ALL temporal attributes on the RELATIONSHIP between two entities, never on an entity node.

Belief-time bounds (when the relationship became / stopped being true):
- "valid_from": ISO-8601 date string (YYYY-MM-DD or YYYY) for when the relationship became true,
  when the text implies a start ("since 2019", "from 2020", "as of January 2024").
- "valid_to": ISO-8601 date string for when it ended; omit it entirely if the relationship is still
  ongoing. Do NOT default it to null.

Real-world event bounds (distinct from valid_from/valid_to):
- "event_time": ISO-8601 date (YYYY-MM-DD) when the underlying event started in the real world.
- "event_time_end": ISO-8601 date (YYYY-MM-DD) when it ended; omit it if the event is still active,
  ongoing, or the end date is unknown.

Rules:
- If no temporal information is present for a relationship, omit these keys entirely.
- Do not fabricate dates. If the text says "in 2020", use "2020-01-01". A year alone is acceptable.

Examples:
- "Alice has been the CTO of Acme since March 2021"
  -> relationship WORKS_FOR {valid_from: "2021-03-01", event_time: "2021-03-01"}
- "Bob served as CFO of Acme from 2018 to 2022"
  -> relationship WORKS_FOR {valid_from: "2018-01-01", valid_to: "2022-12-31",
     event_time: "2018-01-01", event_time_end: "2022-12-31"}
- "Apple acquired a startup in March 2025"
  -> relationship ACQUIRED {event_time: "2025-03-01"}
- "John knows Mary" (no date)
  -> relationship KNOWS {}   (no temporal keys)"""


def temporal_prompt_steering() -> str:
    """The relationship-temporal extraction steering block, appended to the prompt prefix when a
    rule opts in (`temporal: true`). Pure constant text — no schema/example slots to format."""
    return _TEMPORAL_STEERING


def normalize_date(value: Any) -> str | None:
    """Normalise an LLM-returned date to YYYY-MM-DD (legacy `_normalize_date`, made pure).

    - None / empty / blank / non-string -> None (never substitute a default).
    - Year-only "YYYY" -> "YYYY-01-01" (start of the year).
    - Anything else -> returned trimmed, unchanged (assumed already ISO-8601).

    The result is only ever used as a property VALUE, never interpolated into Cypher.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if _YEAR_ONLY.fullmatch(stripped):
        return f"{stripped}-01-01"
    return stripped


def normalize_temporal_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `properties` with the temporal keys normalised + falsy values dropped.

    Coerces each of `valid_from`/`valid_to`/`event_time`/`event_time_end` through `normalize_date`
    (year-only -> full date) and REMOVES any whose value is falsy/blank, so the written edge carries
    no empty/None temporal property where the field is absent. Non-temporal keys pass through
    untouched. Pure — does not mutate the input.
    """
    out: dict[str, Any] = {}
    for key, raw in properties.items():
        if key in TEMPORAL_KEYS:
            normalized = normalize_date(raw)
            if normalized is not None:
                out[key] = normalized
            continue
        out[key] = raw
    return out
