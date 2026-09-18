"""The JSON object a team member answered with (domain layer).

A member's final answer arrives as the model wrote it: prose, a fenced block, or bare JSON, under
the dispatch envelope's ``output``. Two callers need the same reading of it and must never disagree
about which object is "the answer":

  * ``services/team_run.py`` lifts the member's DECLARED keys onto the envelope the next member
    receives (#697);
  * ``domain/member_artifact.py`` builds the document the platform saves at settle (#1137/#1142).

The peel lived in ``team_run.py`` (as ``_parse_member_object``) until #1142 gave the domain layer a
second caller. It moved down here rather than being copied, because a second parser is a second
answer to "what did the member say" and the two would drift; ``team_run.py`` imports it from here
and its behaviour is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any


def parse_member_object(
    output: Any, *, declared_keys: Sequence[str] | None = None
) -> dict[str, Any]:
    """The JSON object a member answered with, or {} when it did not answer with one.

    A real model wraps its JSON in prose or a fence, so the object is PEELED rather than parsed
    whole (the same reason ``validate_draft`` peels the drafter's reply). A real reply can also
    carry MULTIPLE top-level JSON objects back to back (e.g. REVIEWER_PROMPT's team JSON followed
    by a separate ``driving_signals`` receipt object) — a single greedy regex spanning first-`{` to
    last-`}` would swallow both and fail to parse. So every well-formed top-level object in the text
    is decoded in order; when ``declared_keys`` is given, the FIRST object carrying ALL of those
    keys wins, otherwise the first object decoded wins. Never raises: a member that answered with
    prose simply declared keys it did not deliver, and the orchestrator fails it on its own contract
    with a readable reason — a parse crash would say nothing.
    """
    if isinstance(output, dict):
        return output
    if not isinstance(output, str):
        return {}
    decoder = json.JSONDecoder()
    first_object: dict[str, Any] | None = None
    start = output.find("{")
    while start != -1:
        try:
            parsed, end = decoder.raw_decode(output, start)
        except json.JSONDecodeError as exc:
            # Skip past wherever the decoder gave up, not one character at a time — a failed
            # attempt has already ruled out this whole span, so re-walking it `{` by `{` is
            # quadratic on brace-dense input.
            start = output.find("{", max(start + 1, exc.pos))
            continue
        if isinstance(parsed, dict):
            if first_object is None:
                first_object = parsed
            if declared_keys and all(key in parsed for key in declared_keys):
                return parsed
        start = output.find("{", end)
    return first_object if first_object is not None else {}
