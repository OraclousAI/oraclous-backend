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
#
# A BACKSLASH ends a URL, and that one character is load-bearing. A member with a declared output
# contract answers with a JSON document, so its Sources list arrives as `…/trends)\n- [B](…)` where
# the newline is the two characters backslash and n, inside a JSON string — not whitespace, and so
# not a delimiter unless it is named one. Without this, every honestly cited link in such an answer
# reads as `…/trends)\n-`, matches nothing the run fetched, and lands in the all-invented case that
# sends the draft back. Live run bc229dd8 was corrected four times for four real pages it had just
# read and died at the token ceiling, 257k tokens spent. Whatever follows a backslash is the
# document's encoding, never part of the address.
#
# BOUNDED (#944 review, HIGH-1): no real citation is anywhere near this long, and the text scanned
# here is the FULL tool result — `_truncate` only shortens what gets PERSISTED, never what this
# regex reads. An unbounded quantifier on attacker-supplied text (a page the member's tool read)
# made `_trim` below quadratic in the match length: 32k trailing `)` measured at 268ms of blocking
# CPU on this loop's own async call stack, with no `await` to yield it — one crafted page stalls
# every concurrent run sharing the worker. The cap removes the unbounded input, and `_trim` below is
# now linear regardless, so neither half of the old quadratic blowup remains.
_MAX_URL_LENGTH = 2048
_URL = re.compile(rf"https?://[^\s<>\"'`\\\]]{{1,{_MAX_URL_LENGTH}}}", re.IGNORECASE)

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

    O(n), not O(n²) (#944 review, HIGH-1). The original counted ``)``/``(`` over the WHOLE
    (shrinking) string on every character removed, and sliced a new string each time — quadratic in
    the match length, measured at 268ms for a 32k-character run of trailing ``)``. Both counts are
    now taken ONCE, decremented as a ``)`` is walked off, and the string is sliced once at the end.
    """
    close_parens = url.count(")")
    open_parens = url.count("(")
    end = len(url)
    while end > 0:
        ch = url[end - 1]
        if ch == ")":
            if close_parens <= open_parens:
                break
            close_parens -= 1
            end -= 1
        elif ch in _TRAILING_PUNCTUATION:
            end -= 1
        else:
            break
    return url[:end]


# The scheme's own default port (#944 review, LOW-8). `https://example.com:443/x` and
# `https://example.com/x` are the same origin, and search connectors routinely hand back the ported
# form; folding it keeps an honest citation from reading as a mismatch and spending a correction.
_DEFAULT_PORT = {"http": 80, "https": 443}

# A percent-escape triplet, so its hex digits can be case-folded (#944 review, LOW-8). RFC 3986
# treats `%2F` and `%2f` as the same octet; a fetched URL and the model's own prose disagree about
# the case constantly, and left un-folded that costs the member a correction for honest provenance.
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def _fold_percent_escapes(value: str) -> str:
    return _PERCENT_ESCAPE.sub(lambda m: m.group(0).upper(), value)


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
    # #944 review, MEDIUM-5: userinfo is a phishing shape, not a normalisation detail. `urlsplit`
    # silently drops it from `.hostname` — `https://arxiv.org@evil.example/x` verifies against
    # `evil.example` while the console renders an anchor that visibly BEGINS "https://arxiv.org@".
    # Fail closed, like every other unreadable input this function refuses rather than guesses at.
    if parts.username or parts.password:
        return None
    try:
        # #944 review, LOW-7: fold to the ASCII form a browser would actually resolve (IDNA/UTS-46),
        # not a bare `.lower()` — a fetched URL in punycode and the same host written in Unicode in
        # the answer must compare equal, or an honest citation spends a correction over encoding. A
        # host that will not encode is not one a browser would resolve either — reject it, same as
        # any other unparseable authority above.
        host = host.encode("idna").decode("ascii")
    except ValueError:
        return None
    # The stdlib `idna` codec does not itself case-fold a label that is already all-ASCII (e.g.
    # "Example.com" round-trips unchanged); the explicit `.lower()` is still required after it.
    host = host.lower().removeprefix("www.")
    if ":" in host:
        # #944 review, LOW-6: an IPv6 literal — `urlsplit` strips the brackets it arrived in, so
        # `http://[::1]:80/x` and `http://[::1:80]/x` would otherwise canonicalise to the identical
        # (wrong) authority `::1:80`. Re-bracket so the literal and an explicit port cannot collide.
        host = f"[{host}]"
    path = parts.path.removesuffix("/") if parts.path != "/" else ""
    path = _fold_percent_escapes(path)
    if port is not None and port != _DEFAULT_PORT.get(scheme):
        authority = f"{host}:{port}"
    else:
        authority = host
    query = f"?{_fold_percent_escapes(parts.query)}" if parts.query else ""
    return f"{scheme}://{authority}{path}{query}"


def _looks_like_a_link(url: str) -> bool:
    """A minimal, LENIENT check for "is this text a link at all" — scheme is http(s) and a hostname
    is present. Deliberately separate from ``_canonical`` (#944 review, MEDIUM-5 fix): a URL that
    fails ``_canonical``'s stricter checks (userinfo, an unencodable host) is not junk prose — it is
    exactly the kind of link this whole module exists to catch, and it must still be extracted and
    reported as UNVERIFIED. Folding the two together silently dropped a userinfo phishing URL from
    the answer's own extracted-links list — worse than the bug it was meant to fix, because nothing
    downstream ever saw it to flag.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def extract_answer_urls(text: str) -> list[str]:
    """Every http(s) URL occurring in ``text``, as written, first-seen order, deduplicated.

    Deduplication is by CANONICAL form when one exists; a URL ``_canonical`` refuses (userinfo, an
    unencodable host) dedupes on its own written form instead — it still has to appear in the
    output, just without the benefit of canonical-form dedup, because there is no comparable form to
    dedup ON. Citing one real page twice — once with a fragment, once without — is one citation, and
    reporting it twice would inflate what a reader is warned about.
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in _URL.finditer(text or ""):
        written = _trim(match.group(0))
        if not _looks_like_a_link(written):
            continue
        key = _canonical(written) or written
        if key in seen:
            continue
        seen.add(key)
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
