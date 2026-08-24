"""Peeling a member's own JSON answer out of its reply (domain layer) — pure.

A member's answer is not the only thing in its output: the prompts ask for a fenced JSON block,
models often wrap it in prose, and #641 appends a grounding receipt as a SECOND object after the
answer. A greedy ``{.*}`` span swallows the answer, the closing fence and the receipt together and
fails to parse.

Lifted out of ``team_draft_service`` (#866) so the intake read-back peels its reader's answer the
same way the op-drafter's is peeled, rather than growing a second, subtly different parser.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def first_json_object(text: str) -> dict[str, Any] | None:
    """The member's OWN answer object, or None if nothing in ``text`` decodes.

    Prefers the fenced block the prompts ask for; otherwise takes the FIRST complete object, which
    is the answer — a grounding receipt is always appended after it.
    """
    decoder = json.JSONDecoder()
    fenced = _FENCED_JSON.search(text)
    if fenced is not None:
        try:
            parsed = json.loads(fenced.group(1))
        except json.JSONDecodeError:
            parsed = None  # a malformed fence falls through to the scan below
        if isinstance(parsed, dict):
            return parsed
    idx = text.find("{")
    while idx != -1:
        try:
            parsed, _end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
        idx = text.find("{", idx + 1)
    return None
