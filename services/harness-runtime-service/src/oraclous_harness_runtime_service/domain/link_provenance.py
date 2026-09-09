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

import bisect
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import idna

# Candidate URLs. Deliberately only `http`/`https` (the allow-list above), and deliberately greedy
# to the next delimiter — the trailing characters a URL cannot really end with are shaved off by
# `_trim` below, which is the only place that judgement lives. `]` is excluded from the ORDINARY
# branch so a markdown label never bleeds into the target; whitespace and quote characters end a URL
# in every real rendering. The BRACKETED branch (below) is the one narrow exception, scoped to the
# authority position only — see #944 review round 3, MEDIUM-G.
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
# #944 review round 3, HIGH-A: the quantifier is UNBOUNDED, not capped. A cap on the regex itself
# made the cap load-bearing for CORRECTNESS, not just performance: a URL longer than the cap matched
# only a PREFIX of what the reader would actually click (`https://arxiv.org<2041 dots>@evil.example
# /pwn` matched as `https://arxiv.org`), `_trim` shaved that prefix, and the checker verified a
# string that was never the destination — reopening the userinfo phishing hole MEDIUM-5 closed, only
# now returning an affirmative "verified" instead of no signal at all. `_trim` is O(n) on its own
# merits (HIGH-1), so an unbounded quantifier here costs nothing extra; `_MAX_URL_LENGTH` is
# enforced in `_canonical` below instead, where an over-length match fails closed to unverified
# rather than being silently truncated into something else.
_MAX_URL_LENGTH = 2048
_URL = re.compile(
    r"https?://\[[0-9A-Fa-f:.]+\][^\s<>\"'`\\\]]*"  # a bracketed IPv6/IPvFuture authority
    r"|https?://[^\s<>\"'`\\\]]+",  # the ordinary case — unchanged apart from the removed cap
    re.IGNORECASE,
)

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

# MAJOR 2 (code-reviewer, PR #977): the registry cap the loop enforces (``domain/loop/tool_use
# .py``'s ``_MAX_FETCHED_URLS``) is the SAME number two other layers need to bound against —
# ``ExecuteHarnessRequest.prior_fetched_urls``'s ``max_length`` and the repository's ordered-union
# truncation. Both used to import the loop's private name directly, reaching across a layer
# boundary for it (the loop is not either layer's dependency). It lives here instead, alongside
# the trace vocabulary above, for the same reason: this module is the shared home neither the loop
# nor its downstream readers own alone. ``tool_use.py``'s ``_MAX_FETCHED_URLS`` aliases this.
MAX_FETCHED_URLS = 2000


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


# security review 5155075040 (m2), PoC Area 4: a registry entry is never something a reader sees
# raw — it is rendered inside a CommonMark angle-bracket target (`expand_source_markers`'s `<…>`)
# specifically BECAUSE that shape cannot be broken out of by `(`, `)` or `[`. A `>` inside the
# entry itself closes that bracket early regardless, and the rest of the string is read back as
# fresh, live markdown — `https://real.example/>)[click](https://evil.example/phish)` reopens the
# phishing shape the angle-bracket form exists to close off. Whitespace and control characters are
# refused for the same reason a bare URL can never contain them (`_URL`'s own char class already
# excludes them from anything EXTRACTED from text) — a registry entry arrives as a caller-supplied
# string, never extracted, so nothing upstream of this function already enforces it.
_FORBIDDEN_URL_CHARS = re.compile(r"[<>\s\x00-\x1f\x7f]")


def _canonical(url: str) -> str | None:
    """The comparable form of one URL, or None when it is not a link this check reads.

    None covers everything that must never accidentally match: a non-http scheme, a hostless string,
    and outright junk. The fetched set is harvested from whatever a third-party tool returned, so
    this has to be total — a malformed entry may not take the run down and may not match either.
    """
    # #944 review round 3, HIGH-A: an over-length match is EXTRACTED (so it still shows up in the
    # answer's own URL list) but never CANONICALISED — it fails closed to unverified rather than
    # being silently truncated into a prefix and verified as if that prefix were the whole address.
    if len(url) > _MAX_URL_LENGTH:
        return None
    if _FORBIDDEN_URL_CHARS.search(url):
        return None
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
    if ":" in host:
        # #944 review, LOW-6: an IPv6/IPvFuture literal — `urlsplit` strips the brackets it arrived
        # in, so `http://[::1]:80/x` and `http://[::1:80]/x` would otherwise canonicalise to the
        # identical (wrong) authority `::1:80`. Re-bracket so the literal and an explicit port
        # cannot collide. IDNA does not apply to an IP literal at all (a colon is never a valid DNS
        # label character), so this branch skips the `idna` encode below entirely rather than
        # feeding it a string it can only reject.
        host = f"[{host.lower()}]"
    else:
        try:
            # #944 review, LOW-7 / round 3 HIGH-B: fold to the ASCII form a BROWSER would actually
            # resolve — UTS-46 non-transitional (the WHATWG URL Standard), via the third-party
            # `idna` package, not the stdlib `idna` codec (IDNA2003 + nameprep). The two standards
            # disagree on a registrable class of characters: the stdlib codec folds `faß.de` to the
            # ASCII string `fass.de`, identical to the unrelated real domain `fass.de`, while a
            # browser resolves `faß.de` to `xn--fa-hia.de` — a DIFFERENT registrable domain. Any
            # real target containing `ss` (`businessinsider.com`, `press.*`, `assets.*`) has such a
            # pre-image, so a URL pointing at an attacker-controlled domain compared equal to a
            # legitimately fetched one and reported VERIFIED. A host this codec refuses is not one a
            # browser would resolve either — reject it, same as any other unparseable authority.
            host = idna.encode(host, uts46=True, transitional=False).decode("ascii")
        except UnicodeError:
            return None
        # The `idna` codec already case-folds during encoding, but a host it passed through
        # unencoded (already all-ASCII) does not get folded by the encode step itself — the
        # explicit `.lower()` is still required.
        #
        # #944 review round 3, LOW-J: a single trailing dot names the DNS root and is the same host
        # as the same name without one (`example.com.` == `example.com`); left un-stripped an
        # honest citation using either form reads as a mismatch against a fetched URL using the
        # other. Stripped after folding, not before — folding a name that already ends in a dot
        # behaves the same either way, but the strip belongs next to the other host normalisation.
        host = host.lower().removeprefix("www.").removesuffix(".")
    path = parts.path.removesuffix("/") if parts.path != "/" else ""
    path = _fold_percent_escapes(path)
    if port is not None and port != _DEFAULT_PORT.get(scheme):
        authority = f"{host}:{port}"
    else:
        authority = host
    query = f"?{_fold_percent_escapes(parts.query)}" if parts.query else ""
    return f"{scheme}://{authority}{path}{query}"


# ``[label](target)`` in EITHER CommonMark shape — a plain target or an angle-bracket one, each
# optionally followed by a quoted title — matched as ONE construct per pass so nested link shapes
# (`[[Source](url)](url)`) resolve from the inside out across repeated passes, the same way the
# label class excluding `[`/`]` keeps a regex engine from ever matching more than the innermost
# well-formed link. Combined with the bare-URL pattern in one alternation so a single scan handles
# both shapes without either double-matching a URL that sits inside a link's own target or missing
# one that does not.
#
# security review 5155075040 (B1): the title is captured INSIDE the target group (rather than left
# dangling after the group, which is what let `See [Source](https://evil "ref").` leak) so
# `_link_target_url` below can recover the link's real target for every one of markdown's several
# equivalent spellings of the same construct.
#
# #975 N1 (security round 2, review 5156136217): the ordinary (non-angle) branch allows ONE level
# of a balanced, unescaped `(...)` pair inside the target — CommonMark's own destination grammar
# does too — so `[Source](javascript:alert(1))` still tokenises as ONE link with target
# `javascript:alert(1)`, rather than the old `[^()]*` splitting it at the first `(` and leaving the
# real, dangerous target unrecognised as a link at all (and therefore unreachable by N1's
# fail-closed rule below, which only ever sees a `label`/`target` match to judge). A pair containing
# a NESTED paren is still not matched by this — the same one-level depth `_trim`'s own balance
# check already lives with for a bare URL's trailing `)`.
_LINK_TARGET = r"<[^<>]*>(?:\s*(?:\"[^\"]*\"|'[^']*'))?|(?:[^()]|\([^()]*\))*"
_STRIP_SCAN = re.compile(
    r"\[(?P<label>[^\[\]]*)\]\((?P<target>" + _LINK_TARGET + r")\)" + "|" + _URL.pattern,
    re.IGNORECASE,
)


def _link_target_url(target_raw: str) -> str:
    """The link's real, as-written target — a CommonMark title and any surrounding whitespace
    removed, and an angle-bracket wrapper (if the model wrote one) stripped (#975 B1, security
    review 5155075040). ``[Source](https://x "title")``, ``[Source](https://x )`` and
    ``[Source](<https://x> "title")`` all report the identical target ``https://x`` — the string a
    bare-URL scan of the same address would report — so a fabricated target can never dodge the
    check just by picking a different one of markdown's equivalent spellings for the same link.
    """
    stripped = target_raw.strip()
    if stripped.startswith("<"):
        end = stripped.find(">")
        return stripped[1:end] if end != -1 else stripped
    title = re.search(r"""\s+(?:"[^"]*"|'[^']*')\s*$""", stripped)
    return stripped[: title.start()].strip() if title else stripped


def _iter_written_urls(text: str) -> Iterable[str]:
    """Walk ``text`` left to right, tokenising ``[label](target)`` spans FIRST (#975 B1, security
    review 5155075040): a link's own target is read within the link's own boundaries — trimmed of an
    optional CommonMark title and surrounding whitespace — never re-scanned as a free-floating bare
    URL past its own closing paren, which is what let a link immediately followed by ordinary prose
    (no separating space), two adjacent links, or a titled/angle-bracket target report the WRONG
    string as the link's target. Whatever the link branch does not consume — everything outside a
    ``[label](target)`` span — is scanned by the bare-URL branch exactly as before.

    A label is never scanned for a URL here: a fabricated LABEL on an otherwise-verified link is
    ``strip_unverified_links``'s ``fetched=`` concern (#975 M1), not this extraction's — reporting
    it here would flag (and spend a correction on) a member that wrote nothing false in its
    answer's own URL list, only in a link's cosmetic display text.

    #975 N2 (security round 2, review 5156136217): a target that does not itself start with
    ``http(s)://`` is not nothing to this check. ``[S](x https://evil.example/r)`` is not valid
    CommonMark — its destination is not a URL — so this construct is not a link at all; it renders
    as literal text carrying the address in full, and GFM autolink literals (what the console
    actually renders through) turn a bare ``https://`` substring into a working anchor regardless
    of what surrounds it. The link-first tokenisation above still consumes the whole
    ``[label](target)`` span (so an adjacent link's own target is never smeared into this one's —
    B1's fix must not regress), but a target it does not recognise as a URL is then scanned ON ITS
    OWN — never the label, which stays outside this function's contract per the paragraph above —
    for any bare literal-scheme URL hiding inside it, so a span the link branch consumed can never
    hide what the un-consumed bare-URL branch would otherwise have caught.
    """
    for match in _STRIP_SCAN.finditer(text):
        label = match.group("label")
        if label is None:
            written = _trim(match.group(0))
            if written:
                yield written
            continue
        target_raw = match.group("target")
        target = _link_target_url(target_raw)
        if target.lower().startswith(("http://", "https://")):
            yield target
            continue
        for embedded in _URL.finditer(target_raw):
            written = _trim(embedded.group(0))
            if written:
                yield written


def extract_answer_urls(text: str) -> list[str]:
    """Every http(s) URL occurring in ``text``, as written, first-seen order, deduplicated.

    Every candidate is reported — there is no longer a lenient pre-filter that can silently drop
    one (#944 review round 3, policy point). Whether a candidate is one the run actually fetched
    is `_canonical`'s question, not extraction's, and something `_canonical` refuses (userinfo, an
    unencodable host, an over-length match) is not junk prose to be dropped — it is exactly the
    shape that must come back UNVERIFIED rather than vanish. Silently dropping it here was the
    same mistake HIGH-A and MEDIUM-G each made in their own way: a control whose default for "I
    cannot parse this" was a skip rather than a warning.

    Deduplication is by CANONICAL form when one exists; a URL ``_canonical`` refuses dedupes on its
    own written form instead — it still has to appear in the output, just without the benefit of
    canonical-form dedup, because there is no comparable form to dedup ON. Citing one real page
    twice — once with a fragment, once without — is one citation, and reporting it twice would
    inflate what a reader is warned about.
    """
    out: list[str] = []
    seen: set[str] = set()
    for written in _iter_written_urls(text or ""):
        key = _canonical(written) or written
        if key in seen:
            continue
        seen.add(key)
        out.append(written)
    return out


def canonical_urls(values: Iterable[str]) -> set[str]:
    """The comparable forms of ``values``, dropping anything that is not a readable http(s) URL."""
    return {c for c in (_canonical(v) for v in values if isinstance(v, str)) if c is not None}


# ── #975: cite-by-reference — the two pure functions the acceptance pass is built from ───────────
#
# #944's check is gated on the member declaring tools, so a tool-less `linker` member — whose whole
# job is attaching sources — ships URLs composed from training data, unflagged. The owner ruled
# (2026-09-09) the strongest pattern: the model cites NUMBERED ENTRIES from a source registry the
# platform holds (``[S1]``, ``[S2]``, …), and the platform prints the real URL. A fabricated link is
# then impossible by construction. The #944 raw-URL check stays as the backstop, but its consequence
# hardens: an unverified raw URL is STRIPPED from the shipped answer, never rendered.
#
# One upper-case ``S``, one or more digits with NO leading zero (``[1-9]\d*`` — ``[S0]``/``[S007]``
# are ordinary text, never a marker), in exactly one pair of brackets.
_MARKER = re.compile(r"\[S([1-9]\d*)\]")

# A fenced code block, non-greedy across (possibly multi-line) content — a marker written as a code
# EXAMPLE (`refs = ['[S1]']`) is not the member citing anything and must never be expanded/flagged.
_FENCE = re.compile(r"```.*?```", re.DOTALL)


def _inside_any(pos: int, spans: list[tuple[int, int]]) -> bool:
    """Whether ``pos`` falls inside one of ``spans``.

    O(log n) via bisect (#975 m1, security review 5155075040 Area 7): ``spans`` comes straight from
    ``re.finditer``, which always yields non-overlapping matches in increasing start order, so it is
    sorted by construction — a fact this takes advantage of rather than re-deriving. The previous
    linear scan cost this once PER CANDIDATE MARKER, making the whole pass O(n*m) in the number of
    markers times URL/fence spans in the text; measured at 1.79s for 8000 interleaved
    ``https://a.example/p [S1]`` pairs. A crafted page a member's tool reads is attacker-supplied
    text with no bound on how many of each it can interleave.
    """
    idx = bisect.bisect_right(spans, pos, key=lambda span: span[0]) - 1
    if idx < 0:
        return False
    start, end = spans[idx]
    return start <= pos < end


def expand_source_markers(text: str, registry: Collection[str]) -> tuple[str, list[int]]:
    """Turn every well-formed ``[Sn]`` naming a usable registry entry into the inline link
    ``[Sn](<url>)``; report every marker that names none as ``unknown`` (1-based, first-seen,
    deduplicated) and remove it from the text — no literal ``[Sn]`` ever ships (the loop's T3
    invariant). ``registry`` is the ordered list of URLs the run really fetched (or was seeded);
    entry ``n`` is ``registry[n-1]``.

    The label is the marker text itself, never page-derived (S5): nothing from a fetched page — not
    its title, not its own anchor text — is ever placed where a reader can see it. The target is
    written in CommonMark angle-bracket form (``<…>``) so a ``(``, ``)`` or ``[`` inside the URL can
    never open or close a markdown construct (S1) — unlike a bare ``(url)`` target, which the
    Wikipedia-disambiguation shape ``_trim`` exists for would break.

    A marker naming NO usable entry — out of range, or an entry ``_canonical`` itself refuses (S1:
    the registration gate should already have kept it out, but expansion re-checks rather than
    trusting a caller-supplied list) — is unknown. Two positions are never markers at all, and are
    left byte-for-byte alone, reported nowhere: inside a raw URL candidate (``[`` is a legal URL
    character, so ``https://x/a[S1`` is one URL span as far as the raw-URL check is concerned, and
    that check will judge the whole span) and inside a fenced code block. A marker written as
    ``[[Sn]]`` (both a preceding and a following bracket) is ordinary text, not a marker, by the
    same "exactly one pair of brackets" rule that makes the shapes above malformed. A marker already
    immediately followed by ``(`` is already the label of an existing inline link — including one
    THIS function itself just wrote — so re-running the pass is idempotent by construction. A marker
    nested inside an existing link's own label (followed by ``]``, not preceded by ``[``) is not
    valid markdown once nested, so it expands to the bare address instead, which the raw-URL pass
    then judges on its own.
    """
    if not text:
        return text, []
    entries = list(registry)
    url_spans = [(m.start(), m.end()) for m in _URL.finditer(text)]
    fence_spans = [(m.start(), m.end()) for m in _FENCE.finditer(text)]

    out: list[str] = []
    unknown: list[int] = []
    seen: set[int] = set()
    last_end = 0
    for match in _MARKER.finditer(text):
        start, end = match.start(), match.end()
        out.append(text[last_end:start])
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        if _inside_any(start, url_spans) or _inside_any(start, fence_spans):
            out.append(match.group(0))  # a URL candidate or fenced code — not a marker here
        elif before == "[" and after == "]":
            out.append(match.group(0))  # `[[Sn]]` — malformed, ordinary text
        elif after == "(":
            out.append(match.group(0))  # already an inline link's own label — never re-expanded
        else:
            n = int(match.group(1))
            usable = n - 1 < len(entries) and _canonical(entries[n - 1]) is not None
            if usable:
                url = entries[n - 1]
                out.append(url if after == "]" else f"[S{n}](<{url}>)")
            else:
                if n not in seen:
                    seen.add(n)
                    unknown.append(n)
                # removed — no literal marker naming nothing ever reaches a reader
        last_end = end
    out.append(text[last_end:])
    return "".join(out), unknown


def _label_carries_fabricated_url(label: str, fetched_canonical: set[str]) -> bool:
    """#975 M1 (security review 5155075040): does ``label`` itself carry an http(s) URL whose
    canonical form is not a registered entry? The TARGET of a link is what ``check_answer_links``
    already judges; the LABEL is what a reader actually SEES, and a member can write
    ``[https://fabricated](https://really-fetched)`` — a genuinely verified target wearing a
    fabricated address as its display text — which ships untouched if only the target is ever
    checked. An entry ``_canonical`` itself refuses is treated as fabricated (fail closed): it is
    not a registered address either way.
    """
    for raw in (m.group(0) for m in _URL.finditer(label)):
        canonical = _canonical(_trim(raw))
        if canonical is None or canonical not in fetched_canonical:
            return True
    return False


def _strip_pass(
    text: str,
    unverified_raw: set[str],
    unverified_canonical: set[str],
    fetched_canonical: set[str],
) -> str:
    out: list[str] = []
    last_end = 0
    for match in _STRIP_SCAN.finditer(text):
        out.append(text[last_end : match.start()])
        label = match.group("label")
        if label is not None:
            target_raw = match.group("target")
            target = _link_target_url(target_raw)
            target_is_url = target.lower().startswith(("http://", "https://"))
            target_canonical = _canonical(target) if target_is_url else None
            if fetched_canonical and target_canonical is None:
                # #975 N1 (MAJOR, security round 2, review 5156136217): with a registry given —
                # the loop's own real acceptance path — a target that is not itself a
                # `_canonical`-accepted http(s) URL can never BE a registry entry, whether it is a
                # scheme this check refuses outright (`javascript:`, `mailto:`) or a shape a
                # browser's own more lenient URL parser resolves differently than a naive string
                # read suggests (`//host/path`, `https:/host/path`, `https:host/path`, a backslash
                # form). Not a regression — this shipped before too — but fixed the same way as
                # every other unreadable input this module refuses: fail closed. The WHOLE link
                # goes, label included — unlike an ordinary fabricated-but-well-formed URL (S4/M1
                # below still keep that label as plain text), there is nothing here safe to leave
                # visible, because the raw target may itself carry a differently-encoded working
                # anchor a naive read would miss entirely (N2, above). The two-argument path
                # (``fetched`` empty) is unchanged — this rule exists only where the loop's own
                # registry is available to fail closed against.
                out.append("")
            elif target_is_url:
                # security review 5155075040 (B1): judged by the URL's CANONICAL form against the
                # canonical set of `unverified` — never raw string equality against a token a
                # DIFFERENT scan produced, which is what let a title, a trailing space, a second
                # adjacent link, or an angle-bracket-plus-title target leak straight through. A
                # target `_canonical` itself refuses (userinfo, over-length) has no canonical form
                # to compare, so it also falls back to the as-written set, matching
                # `check_answer_links`'s report AS WRITTEN.
                target_bad = target in unverified_raw or (
                    target_canonical is not None and target_canonical in unverified_canonical
                )
                if target_bad:
                    # S4: a label that ITSELF carries an http(s) URL takes the whole link with it
                    # — `[https://fab](https://fab)` stripped to just its label would leave the
                    # fabricated address on the reader's screen as text, and the console linkifies
                    # a bare URL right back into an anchor. Ordinary prose in the label survives.
                    out.append("" if _URL.search(label) else label)
                elif _label_carries_fabricated_url(label, fetched_canonical):
                    out.append("")  # M1: verified target, fabricated label — whole link goes
                else:
                    out.append(match.group(0))  # a verified (or untouched) link survives intact
            else:
                # #975 N2 (security round 2, review 5156136217): the target itself is not a URL,
                # but the raw span between the parens may still CARRY a literal-scheme URL a naive
                # reader would follow — the same gap `_iter_written_urls` closes for extraction,
                # mirrored here so a span this tokeniser consumed as a link can never hide, on the
                # SHIPPED text, what an un-consumed bare-URL scan would already have stripped.
                # (Only reached with `fetched` empty — a non-empty registry already fails this
                # whole shape closed above, N1.)
                target_bad = any(
                    written in unverified_raw
                    or (
                        (canon := _canonical(written)) is not None and canon in unverified_canonical
                    )
                    for written in (_trim(m.group(0)) for m in _URL.finditer(target_raw))
                    if written
                )
                if target_bad:
                    out.append("" if _URL.search(label) else label)
                elif _label_carries_fabricated_url(label, fetched_canonical):
                    out.append("")
                else:
                    out.append(match.group(0))
        else:
            written = _trim(match.group(0))
            canonical = _canonical(written)
            bad = written in unverified_raw or (
                canonical is not None and canonical in unverified_canonical
            )
            out.append(match.group(0)[len(written) :] if bad else match.group(0))
        last_end = match.end()
    out.append(text[last_end:])
    return "".join(out)


def strip_unverified_links(
    text: str, unverified: Collection[str], *, fetched: Collection[str] = ()
) -> str:
    """Remove every occurrence of each URL in ``unverified`` (``LinkCheckResult.unverified`` — the
    answer's URLs AS WRITTEN) from ``text``. A markdown link loses its target and keeps its label as
    plain text, unless the label itself carries an http(s) URL, in which case the whole link goes
    (S4); a bare URL is removed in place, any trailing sentence punctuation `_trim` would have
    shaved off left untouched. Span-based, never ``str.replace`` (T7: stripping ``…/a`` must never
    damage the longer ``…/ab``). A link/bare-URL is judged by the CANONICAL form of the URL(s) it
    actually carries, never by raw string equality against a token a different scan produced
    (security review 5155075040, B1).

    ``fetched`` — the run's full registry, keyword-only, defaulting to empty — is #975 M1: a link
    whose TARGET verifies but whose LABEL carries a different, unregistered URL ships untouched
    under target-only judgement, because that URL never appears in ``unverified`` at all (a label is
    never scanned for the answer's own URL list — see ``extract_answer_urls``). Passing the registry
    here is what lets the label be judged too, on its own terms, independent of the target.

    **#975 N1 ruling (security round 2, review 5156136217).** With ``fetched`` given — the loop's
    real, shipped acceptance path — a link whose target is not itself a ``_canonical``-accepted
    http(s) URL present in ``fetched`` is dropped WHOLE, label included, regardless of what
    ``unverified`` says about it: a scheme-relative target (``//host/path``), a malformed-scheme
    target a browser's own URL parser normalises back to ``https://`` (``https:/host``,
    ``https:host``), a backslash-as-slash target, and a refused scheme (``javascript:``,
    ``mailto:``) are none of them ever extractable as an http(s) URL in the first place, so none of
    them can ever appear in ``unverified`` — a check gated on ``unverified`` membership alone would
    let every one of them through as a working anchor. This is the shipped path's own fail-closed
    default, the same posture ``_canonical`` already takes for everything else it cannot read; it
    is **not** a widening of when an ordinary, well-formed-but-unregistered URL's label survives
    (S4 above, unchanged) — only a target with no readable http(s) form at all takes its label with
    it. The **two-argument path** (``fetched`` empty, the default) is unaffected: this rule is
    reachable only where a real registry exists to fail closed against, matching #944's original
    ruling that flagging semantics without a registry are `check_answer_links`'s question, not this
    function's.

    Runs to a fixpoint before returning — a nested shape like ``[[Source](url)](url)`` only exposes
    its outer link once the inner one is gone, so one linear scan is not enough on its own.
    """
    if not text or (not unverified and not fetched):
        return text
    unverified_raw = set(unverified)
    unverified_canonical = canonical_urls(unverified)
    fetched_canonical = canonical_urls(fetched)
    # MINOR (code-reviewer, PR #977): terminates because every pass either strictly SHORTENS the
    # text (a stripped label/target/URL always removes at least one character) or makes NO CHANGE
    # at all, in which case the loop returns immediately — it can never cycle between two distinct
    # non-fixpoint states. Bounded by the number of spans `_STRIP_SCAN` can ever extract from the
    # (monotonically shrinking) text, so the pass count is finite even for a pathologically nested
    # shape like `[[[Source](url)](url)](url)`.
    while True:
        stripped = _strip_pass(text, unverified_raw, unverified_canonical, fetched_canonical)
        if stripped == text:
            return text
        text = stripped


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
