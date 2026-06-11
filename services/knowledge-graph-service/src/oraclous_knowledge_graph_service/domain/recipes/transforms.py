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

import re
from collections.abc import Callable
from urllib.parse import urlsplit

# Trailing legal-suffix tokens stripped by `canonical` so a company's surface variants collapse to
# ONE canonical key. Compared casefolded with interior `.` removed (so `B.V.`→`bv`, `b.v`→`bv`); the
# strip is applied REPEATEDLY from the tail so stacked suffixes (`Eurail Group Holding`) all fall
# away. Generic corporate-form + grouping words only — never a distinguishing brand token.
_LEGAL_SUFFIXES = frozenset(
    {
        "bv",
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "gmbh",
        "ag",
        "sa",
        "co",
        "corp",
        "corporation",
        "plc",
        "nv",
        "oy",
        "ab",
        "as",
        "group",
        "holding",
        "holdings",
    }
)
# A bare domain has no whitespace and at least one interior dot before the first `/` — `eurail.com`,
# `www.eurail.com` reduce to their hostname stem (`eurail`), but a multi-word phrase like
# `Eurail Group` (has a space) or `Mr. Smith` (dot followed by a space) is NOT a bare domain.
_BARE_DOMAIN = re.compile(r"^[^\s/]+\.[^\s/]+$")
# Surrounding punctuation stripped from the final canonical token (keeps interior word characters).
_STRIP_PUNCT = " \t\r\n.,;:!?\"'`()[]{}<>-_/\\|"


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


def _strip_legal_suffix(token: str) -> str:
    """Casefolded, interior-`.`-removed comparison key for a legal-suffix match (`B.V.`→`bv`)."""
    return token.casefold().replace(".", "")


def _canonical(value: str) -> str:
    """Collapse an entity NAME's surface variants to one canonical key (recipe enrichment Slice 4).

    PURE `str -> str` (no I/O). The single deterministic rule the resolve-on-write path keys entity
    nodes by, so `Eurail B.V.`, `eurail.com`, `Eurail Group` all canonicalise to `eurail` and MERGE
    onto ONE node — while distinct names stay distinct (`Interrail`↛`eurail`, `SNCF`↛`SBB`):

      1. trim + casefold the whole value;
      2. if it is a BARE DOMAIN (`eurail.com`, `www.eurail.com` — no whitespace, an interior dot),
         reduce to its hostname STEM (`_host` strips `www.`/port → `eurail.com`, then drop the TLD
         labels → `eurail`); a multi-word phrase is never treated as a domain;
      3. otherwise tokenise on whitespace and repeatedly drop a trailing legal-suffix token
         (`bv`/`inc`/`ltd`/`group`/`holding`/...; compared casefolded with interior `.` removed, so
         `B.V.`→`bv`), so stacked suffixes (`Eurail Group Holding`) all fall away;
      4. strip surrounding punctuation off the result and collapse internal whitespace.

    An all-suffix or empty value canonicalises to `""` (an empty identity — skipped + warned by the
    caller, like any empty field), so a bare `Ltd` never collapses distinct companies onto nothing.
    """
    text = " ".join(value.strip().casefold().split())
    if not text:
        return ""
    if _BARE_DOMAIN.match(text):
        host = _host(text)  # `www.eurail.com` → `eurail.com`, port stripped, lowercased
        if host:
            # The hostname stem: drop the TLD label(s) so `eurail.com`/`eurail.co.uk` → `eurail`.
            stem = host.split(".")[0]
            return stem.strip(_STRIP_PUNCT)
    tokens = text.split()
    while tokens and _strip_legal_suffix(tokens[-1]) in _LEGAL_SUFFIXES:
        tokens.pop()
    canonical = " ".join(tokens).strip(_STRIP_PUNCT)
    return " ".join(canonical.split())


# The named transform registry: a recipe references a transform by key; the engine applies it to a
# read value via `apply_transform`. Keep these pure `str -> str` — no I/O, no driver, no LLM.
TRANSFORMS: dict[str, Callable[[str], str]] = {
    "host": _host,
    "lower": _lower,
    "strip_www": _strip_www,
    "canonical": _canonical,
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


def canonical(value: str) -> str:
    """Public alias for the `canonical` transform — the resolution path (Slice 4) calls it directly
    to derive an entity's canonical key (`Eurail B.V.`/`eurail.com`/`Eurail Group` → `eurail`)."""
    return _canonical("" if value is None else str(value))


__all__ = [
    "TRANSFORMS",
    "RecipeTransformError",
    "apply_transform",
    "canonical",
    "is_known_transform",
]
