"""Deterministic value transforms (ORAA-4 §21 domain layer — pure `str -> str`, no I/O).

A *transform* is a named, pure function the recipe engine applies to the raw value read from a
field BEFORE it is used (as an identity component or a property value). Transforms are general —
not EURail-specific — so any recipe can derive a finer/normalised value from a structured field
(recipe enrichment Slice 1, oraclous-backend #269). The engine interprets; the schema is data;
the transform is the pure derivation primitive.

`host` returns the URL's *hostname* (lowercased, leading ``www.`` stripped). It is intentionally
the bare hostname, not a registrable domain: ``a.eurail.com`` and ``eurail.com`` are distinct
hosts here. Extracting the registrable domain (the public-suffix-list eTLD+1, so
``shop.co.uk`` → ``co.uk``-aware) is a future refinement that would add a PSL dependency; until
then `host` is the deterministic, dependency-free hostname.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit


class RecipeTransformError(ValueError):
    """A transform was requested by name that is not in the registry."""


def _host(value: str) -> str:
    """The hostname of a URL: lowercased, leading ``www.`` stripped, ``""`` when there is no host.

    Defensive about a missing scheme — ``eurail.com/path`` has no ``//`` so ``urlsplit`` would read
    it as a path, not a netloc; we prepend ``//`` in that case so the authority is parsed. A value
    with no host at all (``""``, ``"   "``, a bare path with no dot before the first ``/``) yields
    ``""`` so the caller treats it as an empty identity (skipped + warned, like any empty field).
    """
    text = value.strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if not parts.netloc and "://" not in text:
        # No scheme: `eurail.com/about` parses as a path, not an authority. Only re-parse with an
        # explicit `//` authority when the first segment LOOKS like a host (contains a dot) — so a
        # bare phrase ("not a url") or a rooted path ("/a/b") yields "" rather than a fake host.
        first_segment = text.split("/", 1)[0]
        if "." not in first_segment:
            return ""
        parts = urlsplit("//" + text)
    hostname = parts.hostname
    if not hostname:
        return ""
    hostname = hostname.lower()
    if hostname.startswith("www."):
        hostname = hostname[len("www.") :]
    return hostname


def _lower(value: str) -> str:
    return value.lower()


def _strip_www(value: str) -> str:
    text = value.strip()
    return text[len("www.") :] if text.lower().startswith("www.") else text


# The named transform registry: a recipe references a transform by key; the engine applies it to a
# read value via `apply_transform`. Keep these pure `str -> str` — no I/O, no driver, no LLM.
TRANSFORMS: dict[str, Callable[[str], str]] = {
    "host": _host,
    "lower": _lower,
    "strip_www": _strip_www,
}


def is_known_transform(name: str) -> bool:
    """Recipe-validation predicate: True iff `name` is a registered transform."""
    return name in TRANSFORMS


def apply_transform(name: str, value: str) -> str:
    """Apply the named transform to `value`. Unknown name → `RecipeTransformError`.

    Pure: the value is coerced to ``str`` (``None`` → ``""``) so a transform never sees a non-str.
    """
    fn = TRANSFORMS.get(name)
    if fn is None:
        raise RecipeTransformError(f"unknown transform {name!r}")
    text = "" if value is None else str(value)
    return fn(text)


__all__ = [
    "TRANSFORMS",
    "RecipeTransformError",
    "apply_transform",
    "is_known_transform",
]
