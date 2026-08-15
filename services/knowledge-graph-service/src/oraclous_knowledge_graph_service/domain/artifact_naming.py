"""Artifact naming + provenance (domain layer — pure, no I/O).

Every agent-written artifact used to arrive as ``inline.txt``: ``graph-ingest`` sends no name and
the internal ingest route supplied one constant default, so a run's whole output shared a single
name. Because the lexical document node is keyed on that name
(``graph_write_repository._node_id`` hashes ``graph_id|document|suffix``), every write in a run
collapsed onto ONE node and overwrote the last — run ``dc167d8e`` landed 7 artifacts and kept 1
(#728).

Two separate concerns, deliberately kept apart:

* the **name** is for a person. It is derived, never a constant, and never depends on the model
  choosing to supply one. Uniqueness is NOT its job — two artifacts may legitimately share a title.
* the **document key** is the graph node's identity. For an agent artifact it is the artifact's own
  id, so two members writing at once cannot collide. For a user upload it stays the filename/path,
  preserving the #522 contract that re-ingesting the same path REPLACES (an idempotent refresh).

The producer (run, member, execution, team) rides ``ProducerRef`` and is metadata, never part of
the name — a person reading ``/v1/artifacts`` should see a title, not a UUID.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

#: How a produced artifact reached the graph. ``user-upload`` keeps filename keying (#522).
ProducerKind = Literal["team-member", "standalone-agent", "user-upload"]

#: The producer kinds that mark content as AGENT-WRITTEN. ``user-upload`` and anything unrecognised
#: are deliberately absent, so the fail-safe direction is the old upload behaviour rather than a
#: silent agent write. Three readers ask this question — the internal ingest door, the worker's
#: document keying, and the citation the ingest mints — and they must never disagree.
AGENT_PRODUCER_KINDS: frozenset[str] = frozenset({"team-member", "standalone-agent"})

#: Longest derived name we store; ``ingestion_jobs.filename`` is String(512), so this leaves room.
MAX_NAME_LEN = 200

#: A leading markdown heading (``#`` .. ``######``), the shape an LLM answer usually opens with.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*#*\s*$")

#: Characters no filesystem, sink or URL path should have to carry.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass(frozen=True)
class ProducerRef:
    """Who produced an artifact. Metadata only — never rendered into the name.

    Known to the RUNTIME at dispatch (the engine binds the run, the member role and the team the
    same way it already binds ``graph_id``), so none of it is supplied by the model.
    """

    kind: ProducerKind
    team_run_id: uuid.UUID | None = None
    member_role: str | None = None
    execution_id: uuid.UUID | None = None
    team_id: str | None = None
    ordinal: int | None = None


def _sanitise(text: str) -> str:
    """Collapse whitespace, drop path/control characters, trim to ``MAX_NAME_LEN``."""
    cleaned = _UNSAFE.sub("", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > MAX_NAME_LEN:
        cleaned = cleaned[:MAX_NAME_LEN].rstrip()
    return cleaned


def title_from_content(content: str) -> str | None:
    """The human title an artifact's own content already carries, or None.

    Prefers a leading markdown heading (``### Urgent Decisions for the Board``), else the first
    non-empty line when it reads like a title rather than a body paragraph. A JSON payload or a
    long opening sentence yields None so the caller falls back to the producer.
    """
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = _HEADING.match(line)
        if heading is not None:
            return _sanitise(heading.group("text")) or None
        # Not a heading: accept only a short, title-shaped opener. A JSON/array payload or a
        # full sentence is body content, not a name.
        if line[0] in "[{<":
            return None
        if len(line) <= MAX_NAME_LEN and not line.endswith("."):
            return _sanitise(line) or None
        return None
    return None


def derive_name(
    content: str,
    *,
    title: str | None = None,
    producer: ProducerRef | None = None,
) -> str:
    """The human-readable name for an artifact. Deterministic, and never ``inline.txt``.

    Resolution order (#728):

    1. an explicit ``title`` the caller supplied;
    2. the title the content already carries (a leading heading / title line);
    3. the producing role plus its run-scoped ordinal (``Adoption-writer output 2``);
    4. ``artifact`` — only reachable with no title, no usable content and no producer.
    """
    if title and _sanitise(title):
        return _sanitise(title)
    from_content = title_from_content(content)
    if from_content:
        return from_content
    if producer is not None and producer.member_role:
        role = _sanitise(producer.member_role) or "member"
        return f"{role} output {producer.ordinal}" if producer.ordinal is not None else role
    return "artifact"


def document_key(*, artifact_id: uuid.UUID, producer: ProducerRef | None) -> str:
    """The graph document node's identity.

    An AGENT artifact keys on its own id, so two members of one run can never merge onto the same
    node. A ``user-upload`` keys on nothing here — the caller keeps passing the filename/path, so
    #522's replace-on-same-path refresh contract is untouched.
    """
    if producer is None or producer.kind == "user-upload":
        raise ValueError("a user upload keys on its filename, not on an artifact id")
    return str(artifact_id)
