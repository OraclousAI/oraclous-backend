"""Leak-safe 422 → VALIDATION_FAILED extraction (ORAA-4 §21 domain layer) — pure, no I/O.

The gateway never relays an upstream error body (§3 rule 8). A 422 is the one case where the
upstream carries *user-correctable* signal worth surfacing — but only the SHAPE of the failure,
never a value. This extracts, from a FastAPI/Pydantic validation body, just the field path (``loc``)
and the error *type* (a fixed machine token, e.g. ``string_too_short``) — NEVER the ``msg``, which
stock Pydantic reflects the submitted value into (``"not a valid email: alice@corp.internal"``).
Both extracted parts are additionally sanitised + length-capped, so a non-conformant upstream cannot
turn this into a relay channel. Returns None when nothing safe is extractable → caller falls back to
the canonical detail-free envelope.
"""

from __future__ import annotations

import json
import re

from oraclous_errors import FieldError

_MAX_FIELDS = 20  # cap the number of surfaced field errors
_MAX_FIELD_LEN = 64  # a field PATH is short; a longer one is suspicious → truncate
_MAX_TOKEN_LEN = 48  # a Pydantic error type is short; truncate defensively
_MAX_BODY = 64 * 1024  # never parse an oversized body
_NON_TOKEN = re.compile(r"[^A-Z0-9_]")
_NON_FIELD = re.compile(r"[^A-Za-z0-9_]")  # a loc part is a field name, never a value
_LEAD = re.compile(r"^[^A-Z]+")


def _loc_to_field(loc: object) -> str | None:
    """Join a Pydantic ``loc`` (list of str/int) into a dotted field path; sanitise + cap."""
    if not isinstance(loc, (list, tuple)):
        return None
    # Sanitise EACH part to the safe field charset: a Pydantic ``loc`` element can be a
    # user-controlled dict key (an email, an internal hostname) that must NEVER surface verbatim.
    parts = [_NON_FIELD.sub("_", str(p)) for p in loc if isinstance(p, (str, int))]
    parts = [p for p in parts if p]
    if not parts:
        return None
    # drop a leading "body"/"query"/"path" wrapper for a cleaner field name
    if parts[0] in ("body", "query", "path") and len(parts) > 1:
        parts = parts[1:]
    field = ".".join(parts)[:_MAX_FIELD_LEN].strip("._")
    return field or None


def _type_to_token(typ: object) -> str | None:
    """Turn a Pydantic error ``type`` into a conformant machine token (``^[A-Z][A-Z0-9_]*$``)."""
    if not isinstance(typ, str) or not typ:
        return None
    token = _NON_TOKEN.sub("_", typ.upper())[:_MAX_TOKEN_LEN]
    token = _LEAD.sub("", token)  # must start with A-Z
    return token or None


def extract_validation_details(raw: bytes) -> list[FieldError] | None:
    """Extract leak-safe ``FieldError``s from an upstream 422 body, or None.

    Accepts the FastAPI/Pydantic shape ``{"detail": [{"loc": [...], "type": "..."}]}``. The string
    shape (``{"detail": "..."}``) is NOT surfaced — a free string cannot be proven value-free here.
    """
    if not raw or len(raw) > _MAX_BODY:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(detail, list):
        return None
    out: list[FieldError] = []
    for item in detail[:_MAX_FIELDS]:
        if not isinstance(item, dict):
            continue
        field = _loc_to_field(item.get("loc"))
        token = _type_to_token(item.get("type"))
        if field and token:
            out.append(FieldError(field=field, issue=token))
    return out or None
