"""Inline-link provenance (domain layer) — issue #944.

``citation_gate.py`` governs the platform's own ``cit_`` ids: an id the platform minted, served to
the run, and can therefore recognise. **A member's free-text answer carries no such id.** It writes
``[Source](https://www.okta.com/blog/…)`` in its own prose, and before this module nothing anywhere
between "a tool call returns a URL" and "the model writes a URL into its final answer" ever read
that URL. The console renders those as real anchors, so a URL composed from training data reached a
person's screen wearing the same clothes as a URL the run actually fetched. Real case: team run
``8ef18ab0``, the ``linker`` role of the "Daily AI News Digest" app.

**Provenance match, never reachability** (ruled on #944, 2026-09-07). A URL in the answer is
verified when it is one the run's OWN tool calls really returned or really read. This module makes
no network request, and the rejected alternative — asking each URL whether it loads — lost twice
over: it passes any real page the run never read (which is most of what a model fabricates: a
plausible slug on a real domain, which a CMS will happily 200), and loading a URL a model composed
points the platform's own server at an address the model chose, which is a server-side request
forgery surface aimed at internal addresses and cloud metadata endpoints.

**This is a pure function over (the draft, the URLs the run fetched)** — no loop, no model, no I/O,
the same posture ``check_answer_citations`` has and for the same reason: a check implemented as a
model instruction is a check that can be talked out of. **What a failed match DOES is loop
behaviour** and lives in ``domain/loop/tool_use.py``: every link unverified sends the draft back to
the member; some verified and some not ships the answer flagged.

Five normalisation rules, and they are load-bearing rather than tidiness. Under the ruled
consequence a false mismatch costs the member a real iteration, so calling
``https://Example.com/a/`` and ``https://example.com/a`` different URLs would spend budget punishing
a member that cited honestly:

1. **Scheme and host fold case; the path does not.** Hosts are case-insensitive by specification and
   paths are not — ``/A`` and ``/a`` are different pages on most servers, and folding them would let
   a member mutate a real path into a wrong one and keep the provenance.
2. **A leading ``www.`` is stripped.** Search results and canonical URLs disagree about it
   constantly, and they are the same page every time.
3. **The fragment is dropped.** ``#section-3`` names a position inside a page the run did fetch.
4. **One trailing slash is stripped from the path.** ``/blog/`` and ``/blog`` are the same page.
5. **The query is KEPT.** ``?id=7`` and ``?id=9`` are different articles.

**The scheme allow-list is ``http`` and ``https``.** ``mailto:``, a relative path, and a bare domain
mentioned in prose are not links a reader clicks through to a cited article; sweeping them in would
flag ordinary writing.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# Candidate URLs. Deliberately only `http`/`https` (the allow-list above), and deliberately greedy
# to the next delimiter — the trailing characters a URL cannot really end with are shaved off by
# `_trim` below, which is the only place that judgement lives. `]` is excluded so a markdown label
# never bleeds into the target; whitespace and quote characters end a URL in every real rendering.
_URL = re.compile(r"https?://[^\s<>\"'`\]]+", re.IGNORECASE)

# Sentence punctuation a URL may sit in front of but never end with. `)` is NOT here: it is the
# markdown link's own closing bracket AND a legitimate character inside a URL
# (`…/Mercury_(planet)`), so it is judged by balance in `_trim` rather than stripped on sight.
_TRAILING_PUNCTUATION = ".,;:!?\"'’”]}>"


# The trace vocabulary this check writes, and the read DTO reads back. It lives here rather than in
# the loop because both sides need it and neither owns the other: the loop records the verdict, the
# execution DTO reports it, and a name private to one of them makes the other import the whole
# module to say the same word.
#
# GATE because this is a governance decision, matching the citation gate's step. The two statuses
# are DISTINCT on purpose — the read side must tell "the member was corrected and then fixed it"
# (nothing wrong with what shipped) from "the answer shipped carrying a bad link" (warn the reader).
# Collapsing them would warn a reader about a link that is not in what they are reading.
LINK_GATE_NAME = "link_provenance"
LINK_CORRECTION_STATUS = "link_correction"
LINK_FLAG_STATUS = "unverified_links"


@dataclass(frozen=True, slots=True)
class LinkCheckResult:
    """``verified``/``unverified`` are the answer's URLs **as the member wrote them**, in first-seen
    order, deduplicated. As-written matters: a correction that names a URL has to name the one the
    member can find in its own draft, and the normalised form is not a string that appears there."""

    passed: bool
    verified: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)


def _trim(url: str) -> str:
    """Shave trailing characters that belong to the sentence rather than to the URL.

    ``See https://example.org/a.`` ends a sentence; ``(see https://example.org/a)`` closes a
    parenthesis; ``[Source](https://example.org/a)`` closes a markdown target. Without this, none of
    the three ever matches anything the run fetched and every honestly-cited answer is flagged.
    A closing parenthesis is removed only when the URL has more of them than it opened, so
    ``…/Mercury_(planet)`` survives intact.
    """
    while url:
        if url[-1] == ")":
            if url.count(")") <= url.count("("):
                break
            url = url[:-1]
        elif url[-1] in _TRAILING_PUNCTUATION:
            url = url[:-1]
        else:
            break
    return url


def _canonical(url: str) -> str | None:
    """The comparable form of one URL, or None when it is not a link this check reads.

    None covers everything that must never accidentally match: a non-http scheme, a hostless string,
    and outright junk. The fetched set is harvested from whatever a third-party tool returned, so
    this has to be total — a malformed entry may not take the run down and may not match either.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return None  # an unparseable authority (a bad port, a malformed IPv6 literal)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        return None
    host = host.removeprefix("www.")
    path = parts.path.removesuffix("/") if parts.path != "/" else ""
    authority = f"{host}:{port}" if port is not None else host
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{authority}{path}{query}"


def extract_answer_urls(text: str) -> list[str]:
    """Every http(s) URL occurring in ``text``, as written, first-seen order, deduplicated.

    Deduplication is by CANONICAL form: citing one page twice — once with a fragment, once without
    — is one citation, and reporting it twice would inflate what a reader is warned about.
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in _URL.finditer(text or ""):
        written = _trim(match.group(0))
        canonical = _canonical(written)
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        out.append(written)
    return out


def canonical_urls(values: Iterable[str]) -> set[str]:
    """The comparable forms of ``values``, dropping anything that is not a readable http(s) URL."""
    return {c for c in (_canonical(v) for v in values if isinstance(v, str)) if c is not None}


def check_answer_links(answer: str, fetched: Collection[str]) -> LinkCheckResult:
    """Split the answer's URLs into the ones the run really fetched and the ones it did not.

    ``fetched`` is what the run's own tool calls returned or read, accumulated across every
    iteration — the loop owns that accumulation, because it is the only place that ever holds a
    tool result in full (the persisted trace truncates them).

    **An answer that links nothing is never a violation**, the same property rev4 of §CITE protects
    for citations: a member that reasons without linking has fabricated nothing, and punishing it
    would push every member toward inventing a link to look compliant.
    """
    fetched_set = canonical_urls(fetched)
    verified: list[str] = []
    unverified: list[str] = []
    for url in extract_answer_urls(answer):
        canonical = _canonical(url)
        (verified if canonical in fetched_set else unverified).append(url)
    return LinkCheckResult(passed=not unverified, verified=verified, unverified=unverified)
