"""The ``Citation`` / ``SourceRef`` / ``Author`` models (§CITE rev3).

One shape, shared by every repo that touches a citation. The JSON Schema under
``packages/citation/contract/`` is the same shape expressed for the frontend; the conformance test
in this package holds the two together, so neither side may drift.

Two boundaries are encoded in the types rather than left to a caller's discipline:

* ``Citation`` is **never partly filled** — every field is required, so a half-citation cannot be
  constructed. A record either carries complete source identity or ``None``.
* ``SourceRef`` is the only citation-shaped thing that may travel **inbound**, and it forbids extra
  keys. ``citation_id`` and ``retrieved_at`` are platform-computed, so a caller supplying either is
  rejected at the boundary rather than silently ignored (threat T1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_serializer, model_validator

#: A source-native version kind, or our own content-hash fallback. Closed on purpose: the field's
#: only job is telling the two apart, which an open set defeats. Contrast ``source_system``, which
#: §CITE says explicitly is NOT a frozen enum.
RevisionKind = Literal[
    "commit",
    "blob_sha",
    "version_id",
    "etag",
    "edited_at",
    "block",
    "content_hash",
]

NonEmpty = Annotated[str, StringConstraints(min_length=1)]
SourceSystem = Annotated[str, StringConstraints(min_length=1, max_length=64)]
AbsoluteUrl = Annotated[str, StringConstraints(pattern=r"^https?://")]


class Author(BaseModel):
    """The SOURCE record's author, as the source exposes them."""

    model_config = ConfigDict(extra="forbid")

    display_name: NonEmpty
    source_handle: NonEmpty | None = None
    email: NonEmpty | None = None

    @model_serializer(mode="plain")
    def _serialise(self) -> dict[str, str]:
        """Omit an unknown handle or email rather than emitting an explicit ``null``.

        The shared JSON Schema types both as ``string`` and requires only ``display_name``, so
        "the source exposes no email" is an ABSENT key, not a null one. Emitting null would make
        the model's own output fail the schema the frontend validates against.
        """
        emitted = {"display_name": self.display_name}
        if self.source_handle is not None:
            emitted["source_handle"] = self.source_handle
        if self.email is not None:
            emitted["email"] = self.email
        return emitted


class Citation(BaseModel):
    """The resolvable source identity of one knowledge record.

    Minted by platform code only — a model may reference a ``citation_id``, it may never author
    one. Every field is required: there is no half-citation, because one is indistinguishable from
    a real citation at a glance and the answer-time gate must be able to say "this record cannot be
    cited" without inspecting five fields.
    """

    model_config = ConfigDict(extra="forbid")

    citation_id: Annotated[str, StringConstraints(pattern=r"^cit_[0-9a-f]{32}$")]
    source_system: SourceSystem
    source_id: NonEmpty
    #: Never null, on any path — the platform falls back to hashing the content it holds.
    revision: NonEmpty
    revision_kind: RevisionKind
    #: Null only where the platform genuinely cannot resolve the source (today: the upload path).
    url: AbsoluteUrl | None
    #: Display label ONLY — never identity.
    title: NonEmpty | None
    author: Author | None
    #: As-of: when this revision was read from the source.
    retrieved_at: datetime
    #: The source's own permission handle. Carried now, unenforced until permission mirroring.
    permission_ref: NonEmpty | None


class SourceRef(BaseModel):
    """What a CONNECTOR emits alongside the content it read — the inbound half of a citation.

    ``revision``/``revision_kind`` are optional inbound: a connector that cannot name a version is
    not rejected at the boundary, because the platform falls back to a content hash. They travel
    together or not at all — a version with no statement of what kind it is cannot be told apart
    from our own fallback, which is the one job ``revision_kind`` has.
    """

    model_config = ConfigDict(extra="forbid")

    source_system: SourceSystem
    source_id: NonEmpty
    revision: NonEmpty | None = None
    revision_kind: RevisionKind | None = None
    url: AbsoluteUrl | None = None
    title: NonEmpty | None = None
    author: Author | None = None
    permission_ref: NonEmpty | None = None

    @model_validator(mode="after")
    def _revision_travels_with_its_kind(self) -> SourceRef:
        if (self.revision is None) != (self.revision_kind is None):
            raise ValueError("revision and revision_kind must be supplied together")
        return self
