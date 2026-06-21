"""Leak-safe 422 → VALIDATION_FAILED + 409 → CREDENTIALS_REQUIRED extraction (domain layer) — pure.

The gateway never relays an error body verbatim (§3 rule 8). A 422 is the one case where there is
*user-correctable* signal worth surfacing — but only the SHAPE of the failure, never a value. This
extracts, from a FastAPI/Pydantic validation error, just the field path (``loc``) and the error
*type* (a fixed machine token, e.g. ``string_too_short`` / ``value_error``) — NEVER the ``msg``,
which stock Pydantic reflects the submitted value into (``"not a valid email: alice@corp.internal"``
or ``"Value error, invalid CORS origin 'evil/'"``). Both extracted parts are additionally sanitised
+ length-capped, so a non-conformant input cannot turn this into a relay channel.

Two entry points share the same sanitisers:
- ``extract_validation_details`` parses a serialised upstream ``{"detail": [...]}`` body (the proxy
  path, #225) — returns None when nothing safe is extractable so the caller falls back to the
  canonical detail-free envelope.
- ``details_from_errors`` takes a FastAPI ``RequestValidationError.errors()`` list directly (the
  gateway's OWN request-body validation, #281) — a model-level validator whose ``loc`` is just
  ``("body",)`` still yields one usable field so the client gets field-level feedback.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from oraclous_errors import FieldError, NeedsCredential

_MAX_FIELDS = 20  # cap the number of surfaced field errors
_MAX_FIELD_LEN = 64  # a field PATH is short; a longer one is suspicious → truncate
_MAX_TOKEN_LEN = 48  # a Pydantic error type is short; truncate defensively
_MAX_BODY = 64 * 1024  # never parse an oversized body
_NON_TOKEN = re.compile(r"[^A-Z0-9_]")
_NON_FIELD = re.compile(r"[^A-Za-z0-9_]")  # a loc part is a field name, never a value
_LEAD = re.compile(r"^[^A-Z]+")
_NON_CRED_TOKEN = re.compile(r"[^A-Za-z0-9_.-]")  # requirement_id/provider charset (no /:@/space)
_LEAD_NON_ALNUM = re.compile(r"^[^A-Za-z0-9]+")  # a token must start alnum (per the envelope RE)
_MAX_REQUIREMENT_LEN = 64
_MAX_PROVIDER_LEN = 48
# The token charset alone still admits an internal-host / private-IP / long-digit shape (dots,
# hyphens and digits are legal). Those would never be a real requirement-type/provider name, so we
# drop them at the edge — the live mirror of the forbidden-substring classes the contract scanner
# enforces in tests (internal_dns_suffix / private_ip / long_digit_run), so no such shape reaches a
# client even though that scanner does not ship to gateway runtime.
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_LONG_DIGIT_RUN_RE = re.compile(r"\d{13,}")
_INTERNAL_SUFFIXES = (".internal", ".local", ".lan", ".corp", ".svc", ".cluster.local", ".intra")


def _looks_sensitive(token: str) -> bool:
    low = token.lower()
    return bool(
        _IPV4_RE.match(token)
        or _LONG_DIGIT_RUN_RE.search(token)
        or low.endswith(_INTERNAL_SUFFIXES)
    )


def _loc_to_field(loc: object, *, body_fallback: bool = False) -> str | None:
    """Join a Pydantic ``loc`` (list of str/int) into a dotted field path; sanitise + cap.

    ``body_fallback`` returns ``"body"`` (not None) when the ``loc`` is the bare ``("body",)`` of a
    model-level validator — so a request-level constraint (the integration-key XOR rule) still
    yields one field-level detail the client can render, rather than being silently dropped.
    """
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
    elif parts == ["body"] and body_fallback:
        # a model-level (``mode="after"``) validator: keep "body" so the detail survives
        return "body"
    field = ".".join(parts)[:_MAX_FIELD_LEN].strip("._")
    return field or None


def _type_to_token(typ: object) -> str | None:
    """Turn a Pydantic error ``type`` into a conformant machine token (``^[A-Z][A-Z0-9_]*$``)."""
    if not isinstance(typ, str) or not typ:
        return None
    token = _NON_TOKEN.sub("_", typ.upper())[:_MAX_TOKEN_LEN]
    token = _LEAD.sub("", token)  # must start with A-Z
    return token or None


def _details_from_items(items: list, *, body_fallback: bool) -> list[FieldError] | None:
    """Convert a list of Pydantic error dicts (``{"loc": [...], "type": "..."}``) to FieldErrors."""
    out: list[FieldError] = []
    for item in items[:_MAX_FIELDS]:
        if not isinstance(item, dict):
            continue
        field = _loc_to_field(item.get("loc"), body_fallback=body_fallback)
        token = _type_to_token(item.get("type"))
        if field and token:
            out.append(FieldError(field=field, issue=token))
    return out or None


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
    return _details_from_items(detail, body_fallback=False)


def details_from_errors(errors: Sequence[Any]) -> list[FieldError] | None:
    """Leak-safe ``FieldError``s from a FastAPI ``RequestValidationError.errors()`` list, or None.

    Same sanitisation as the proxy path: only ``loc`` (field path) and ``type`` (machine token) are
    used, never ``msg``/``ctx`` (which Pydantic reflects the submitted value into). The
    ``body_fallback`` keeps the bare-``body`` ``loc`` of a model-level validator so a request-level
    constraint still surfaces one field-level detail.
    """
    if not isinstance(errors, list):
        return None
    return _details_from_items(errors, body_fallback=True)


def _cred_token(value: object, *, limit: int) -> str | None:
    """Sanitise a credential ``requirement_id``/``provider`` to the leak-safe token charset + cap.

    Strips any character outside ``[A-Za-z0-9_.-]`` (so a URL, an internal host:port, an ``@``, or a
    secret cannot survive), drops a leading non-alnum so the result matches the envelope's
    ``^[A-Za-z0-9]...`` pattern, and caps the length. Returns None when nothing usable remains.
    """
    if not isinstance(value, str):
        return None
    token = _LEAD_NON_ALNUM.sub("", _NON_CRED_TOKEN.sub("", value))[:limit]
    if not token or _looks_sensitive(token):
        return None
    return token


def extract_needs_credential(raw: bytes) -> NeedsCredential | None:
    """Extract a leak-safe ``needs_credential`` token from an upstream 409 body, or None.

    A capability-registry credential miss returns a 409 whose body carries
    ``needs_credential: {requirement_id, provider}`` (the only place the missing requirement is
    named). This surfaces ONLY those two machine tokens, sanitised + capped — it NEVER surfaces
    ``login_url``/``missing_scopes`` (a URL could carry an internal host). Returns None — so the
    proxy falls back to the canonical CONFLICT envelope — when no safe token is extractable, so a
    genuine state-conflict 409 (no credential signal in its body) is never mislabelled.
    """
    if not raw or len(raw) > _MAX_BODY:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return None
    nc = body.get("needs_credential") if isinstance(body, dict) else None
    if not isinstance(nc, dict):
        return None
    requirement_id = _cred_token(nc.get("requirement_id"), limit=_MAX_REQUIREMENT_LEN)
    provider = _cred_token(nc.get("provider"), limit=_MAX_PROVIDER_LEN)
    if requirement_id is None or provider is None:
        return None
    return NeedsCredential(requirement_id, provider)
