"""#944 — the inline-link provenance check's VERDICTS, as a pure function.

The gap this closes is not the one ``citation_gate.py`` closes. That gate governs the platform's own
``cit_`` ids: an id the platform minted, served to the run, and can therefore recognise. A member's
free-text answer carries no such id. It writes ``[Source](https://www.okta.com/blog/…)`` in its own
prose, and until this lands **nothing anywhere in the pipeline reads that URL**. The frontend now
renders those as real anchors (oraclous-frontend #281 follow-up), so a URL the model composed from
training data reaches a person's screen wearing the same clothes as a URL the run actually fetched.

**Provenance match, not reachability — RULED by the owner on #944 (2026-09-07).** The check compares
the URLs in the answer against the URLs the run's OWN tool calls actually returned. It makes no
network request of its own. Two reasons the rejected alternative (asking each URL whether it loads)
lost:

* A load check passes any real page the run never read, which is most of what a model fabricates —
  it composes plausible URLs on real domains, and a CMS that 200s an unknown slug launders them all.
* Loading a URL a model composed points OUR server at an address the model chose. That is a
  server-side request forgery surface aimed at internal addresses and cloud metadata endpoints, and
  it is not worth opening for a weaker signal.

**What this file does NOT decide.** What a failed match DOES is loop behaviour — the correction, the
flag, the terminal — and lives in ``test_link_provenance_loop.py``. This file is the pure function
over (the answer, the URLs the run fetched), with no loop, no model, and no I/O, the same posture
``check_answer_citations`` has and for the same reason: a gate implemented as a model instruction is
a gate that can be talked out of.

Five normalisation rules are pinned here because a false mismatch is expensive under the ruled
consequence. An answer whose every link fails goes BACK to the member and costs it an iteration, so
a check that calls ``https://Example.com/a/`` and ``https://example.com/a`` different URLs would
spend real budget punishing a member that cited honestly:

1. **Scheme and host are case-insensitive; the path is not.** Hosts are case-insensitive by
   specification and paths are not — ``/A`` and ``/a`` are different pages on most servers.
2. **A leading ``www.`` is stripped from the host.** Search results and canonical URLs disagree
   about it constantly, and they are the same page every time.
3. **The fragment is dropped.** ``#section-3`` names a position within a page the run did fetch.
4. **One trailing slash is stripped from the path.** ``/blog/`` and ``/blog`` are the same page.
5. **The query string is KEPT.** ``?id=7`` and ``?id=9`` are different pages, and dropping it would
   let a member change the article and keep the provenance.

**The scheme allow-list is ``http`` and ``https``, and nothing else.** ``mailto:``, a relative path,
and a bare domain with no scheme are not links a person clicks through to a cited article, and
sweeping them in would flag ordinary prose that happens to mention a domain name.

``link_provenance`` is imported function-locally, never at module level
(``.claude/rules/tests-seam-imports.md``): the module does not exist until the ``[impl]`` lands, and
a module-level import would abort collection for every suite in the repo.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

# The real fabricated citation from team run 8ef18ab0 (the `linker` role, "Daily AI News Digest").
# It is a well-formed URL on a real company's real blog host, and it is exactly what this check has
# to catch: nothing about its SHAPE is wrong, so only provenance can tell it from a real one.
_FABRICATED = "https://www.okta.com/blog/2023/10/okta-ai-token-costs"
_REAL = "https://arstechnica.com/ai/2026/09/model-costs-fall-again/"


def _check(answer: str, fetched: Any) -> Any:
    from oraclous_harness_runtime_service.domain.link_provenance import check_answer_links

    return check_answer_links(answer, fetched)


def _urls(answer: str) -> Any:
    from oraclous_harness_runtime_service.domain.link_provenance import extract_answer_urls

    return extract_answer_urls(answer)


# --- criterion 1: the shape the issue reports — a markdown link the run never fetched ---------


async def test_a_markdown_link_the_run_never_fetched_is_unverified() -> None:
    answer = f"Token costs fell sharply this quarter. [Source]({_FABRICATED})"
    result = _check(answer, [_REAL])
    assert result.unverified == [_FABRICATED]
    assert result.verified == []
    assert result.passed is False


async def test_a_markdown_link_the_run_did_fetch_is_verified() -> None:
    answer = f"Token costs fell sharply this quarter. [Source]({_REAL})"
    result = _check(answer, [_REAL])
    assert result.verified == [_REAL]
    assert result.unverified == []
    assert result.passed is True


# --- criterion 2: an answer with no links at all is never a violation ------------------------


async def test_an_answer_with_no_links_passes_even_though_the_run_fetched_pages() -> None:
    # The mirror of the citation gate's most important property. A member that reasons without
    # linking has not fabricated anything, and a check that punishes it would push every member
    # toward inventing a link to look compliant — the exact failure this exists to prevent.
    result = _check("The two figures do not contradict each other.", [_REAL])
    assert result.passed is True
    assert result.unverified == []
    assert result.verified == []


async def test_an_answer_with_no_links_and_no_fetches_passes() -> None:
    result = _check("A 30-day notice period is the market standard.", [])
    assert result.passed is True
    assert result.unverified == []


# --- criterion 3: a bare URL in prose counts, not only a markdown link -----------------------


async def test_a_bare_url_in_prose_is_checked_too() -> None:
    # A model does not always reach for markdown. An unwrapped URL is the same claim and the
    # frontend linkifies it the same way, so a markdown-only reader would miss half the surface.
    result = _check(f"See {_FABRICATED} for the breakdown.", [_REAL])
    assert result.unverified == [_FABRICATED]


async def test_sentence_punctuation_after_a_bare_url_is_not_part_of_it() -> None:
    # "…costs." — the full stop ends the sentence, not the URL. Without this the URL never matches
    # anything the run fetched and every honestly-cited answer is flagged.
    result = _check(f"The breakdown is at {_REAL}.", [_REAL])
    assert result.verified == [_REAL]
    assert result.unverified == []


async def test_a_trailing_close_paren_after_a_bare_url_is_not_part_of_it() -> None:
    result = _check(f"The breakdown (see {_REAL}) is clear.", [_REAL])
    assert result.unverified == []


# --- criterion 4: the five normalisation rules -----------------------------------------------


async def test_the_host_matches_case_insensitively() -> None:
    result = _check("[Source](https://ArsTechnica.com/ai/2026/09/model-costs-fall-again/)", [_REAL])
    assert result.unverified == []


async def test_the_path_matches_case_SENSITIVELY() -> None:
    # /Ai/ is a different page from /ai/ on most servers. Folding case here would let a member
    # mutate a real path into a wrong one and keep the provenance.
    result = _check("[Source](https://arstechnica.com/AI/2026/09/model-costs-fall-again/)", [_REAL])
    assert len(result.unverified) == 1


async def test_a_leading_www_is_stripped_from_the_host() -> None:
    fetched = "https://okta.com/blog/2023/10/real-post"
    result = _check("[Source](https://www.okta.com/blog/2023/10/real-post)", [fetched])
    assert result.unverified == []


async def test_a_fragment_is_dropped() -> None:
    result = _check(f"[Source]({_REAL}#cost-table)", [_REAL])
    assert result.unverified == []


async def test_one_trailing_slash_is_stripped_from_the_path() -> None:
    result = _check(
        "[Source](https://arstechnica.com/ai/2026)", ["https://arstechnica.com/ai/2026/"]
    )
    assert result.unverified == []


async def test_the_query_string_is_KEPT() -> None:
    # ?id=7 and ?id=9 are different articles. Dropping the query would let a member keep the
    # provenance of a page the run really read while pointing the reader at a different one.
    result = _check(
        "[Source](https://example.org/article?id=9)", ["https://example.org/article?id=7"]
    )
    assert len(result.unverified) == 1


# --- criterion 5: the scheme allow-list ------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "Write to sales at mailto:team@example.org for the figures.",
        "The file is at ftp://files.example.org/report.pdf",
        "See the archive at /reports/2026/q3 for the breakdown.",
        "Coverage came from arstechnica.com and okta.com this quarter.",
    ],
)
async def test_a_non_http_target_is_not_a_link_this_check_reads(answer: str) -> None:
    result = _check(answer, [])
    assert result.passed is True
    assert result.unverified == []


# --- criterion 6: reporting shape -------------------------------------------------------------


async def test_every_unverified_url_is_reported_not_just_the_first() -> None:
    # The reader's screen has to say WHICH links are unverified. Stopping at the first would
    # understate the problem on precisely the worst answers — the ones that invented several.
    second = "https://www.forbes.com/sites/nobody/2026/01/01/invented/"
    answer = f"[A]({_FABRICATED}) and [B]({second}) and [C]({_REAL})"
    result = _check(answer, [_REAL])
    assert result.unverified == [_FABRICATED, second]
    assert result.verified == [_REAL]


async def test_a_url_cited_twice_is_reported_once() -> None:
    answer = f"[Source]({_FABRICATED}) … and again at {_FABRICATED}"
    result = _check(answer, [])
    assert result.unverified == [_FABRICATED]


async def test_a_url_is_reported_as_the_member_wrote_it() -> None:
    # A correction that names the URL has to name the one the member can find in its own draft.
    # Reporting the normalised form would send it hunting for a string that is not there.
    written = "https://WWW.Okta.com/blog/2023/10/okta-ai-token-costs#intro"
    result = _check(f"[Source]({written})", [])
    assert result.unverified == [written]


async def test_first_seen_order_is_preserved() -> None:
    a = "https://example.org/a"
    b = "https://example.org/b"
    assert _urls(f"[B]({b}) then [A]({a}) then [B again]({b})") == [b, a]


# --- criterion 7: the answer is often a JSON document, not loose prose -------------------------
#
# Found on the DEPLOYED stack, not by reading the code. A member with a declared output contract
# answers with a JSON document, so its Sources list arrives as `…/trends)\n- [B](…)` — where the
# newline is the two characters backslash and n, inside a JSON string. A matcher that runs to the
# next space swallows `)\n-` into the URL, so EVERY honestly-cited link fails to match what the run
# fetched, the member is corrected on every attempt, and the run dies at the token ceiling. One live
# run burned 257k tokens that way. A backslash therefore ends a URL.


async def test_a_url_inside_a_json_encoded_answer_is_read_correctly() -> None:
    answer = (
        f'{{"summary": "Prices fell.\\n\\nSources:\\n- [Ars]({_REAL})\\n- [Okta]({_FABRICATED})"}}'
    )
    result = _check(answer, [_REAL])
    assert result.verified == [_REAL]
    assert result.unverified == [_FABRICATED]


async def test_a_backslash_escape_never_becomes_part_of_a_url() -> None:
    # The general rule behind the case above: whatever follows a backslash is the document's
    # encoding, never the address. `\"` closing a JSON string is the same trap as `\n`.
    assert _urls(r'{"url": "https://example.org/a\n- next"}') == ["https://example.org/a"]
    assert _urls(r'{"url": "https://example.org/a\"}') == ["https://example.org/a"]


# --- criterion 8: the provenance set is matched the same way ----------------------------------


async def test_a_fetched_url_is_normalised_before_matching() -> None:
    # The run's own tool results are no tidier than the model's prose. A fetched URL carrying a
    # tracking fragment and a trailing slash still has to match the clean one in the answer.
    result = _check(
        f"[Source]({_REAL})", ["https://ArsTechnica.com/ai/2026/09/model-costs-fall-again/#top"]
    )
    assert result.unverified == []


async def test_a_malformed_entry_in_the_fetched_set_is_ignored_not_crashed() -> None:
    # The fetched set is harvested from whatever a third-party tool returned. It must never be able
    # to take the run down, and a junk entry must never accidentally match anything.
    result = _check(f"[Source]({_FABRICATED})", ["", "not a url", "://broken", _REAL])
    assert result.unverified == [_FABRICATED]


async def test_an_invalid_port_in_the_fetched_set_is_ignored_not_crashed() -> None:
    # `urlsplit(...).port` itself raises ValueError for a port that does not fit — the specific
    # branch `_canonical`'s `except ValueError` exists to catch, exercised directly rather than only
    # folded into the general malformed-entry case above.
    result = _check(f"[Source]({_REAL})", ["https://example.org:99999/x"])
    assert result.unverified == [_REAL]


# --- #944 review, HIGH-1: `_trim` must stay bounded on attacker-supplied text -------------------


async def test_a_url_containing_a_balanced_parenthesis_pair_keeps_its_closing_paren() -> None:
    # `_trim`'s own comment cites this exact case: a `)` is stripped only when the URL carries MORE
    # closes than opens. Wikipedia's disambiguation suffix is the canonical real-world example, and
    # the KEEP path this exercises had no direct test before this change.
    wiki = "https://en.wikipedia.org/wiki/Mercury_(planet)"
    result = _check(f"[Source]({wiki})", [wiki])
    assert result.verified == [wiki]
    assert result.unverified == []


async def test_a_long_run_of_trailing_punctuation_is_trimmed_in_bounded_time() -> None:
    # HIGH-1: `_trim` counted `)`/`(` over the WHOLE shrinking string on every character it removed
    # — quadratic in the match length. A page a member's tool reads is attacker-supplied text with
    # no length bound, and this loop's own call stack has no `await` on this path, so a crafted page
    # stalled every concurrent run sharing the worker (measured: 268ms at 32k trailing `)`). Bounded
    # now on both sides (a linear `_trim`, and the regex capped at `_MAX_URL_LENGTH`) — this must
    # stay fast and must not corrupt an ordinary URL sitting in front of the run of punctuation.
    long_answer = f"[Source]({_REAL}{')' * 20000}"
    started = time.monotonic()
    result = _check(long_answer, [_REAL])
    assert time.monotonic() - started < 0.5
    assert result.verified == [_REAL]
    assert result.unverified == []


# --- #944 review, MEDIUM-5 / LOW-6/7/8: normalisation collisions found at review -----------------


async def test_a_url_with_userinfo_never_verifies_even_against_the_real_host() -> None:
    # MEDIUM-5: `urlsplit(...).hostname` silently drops userinfo, so `https://arxiv.org@evil.example
    # /paper` would otherwise VERIFY against a fetched `https://evil.example/paper` while the
    # console renders an anchor that visibly BEGINS "https://arxiv.org@" — the classic userinfo
    # phishing shape, blessed by the platform's own provenance signal. Fail closed instead, exactly
    # like every other unreadable input this function refuses rather than guesses at.
    phishing = "https://arxiv.org@evil.example/paper"
    result = _check(f"[Source]({phishing})", ["https://evil.example/paper"])
    assert result.unverified == [phishing]


async def test_an_ipv6_literal_and_a_bracket_smuggled_port_do_not_collide() -> None:
    # LOW-6: `urlsplit` strips the brackets an IPv6 literal arrived in. Without re-bracketing them,
    # `http://[::1]:80/x` (host `::1`, port 80) and `http://[::1:80]/x` (host `::1:80`, no port)
    # canonicalise to the identical (wrong) authority `::1:80` and would cross-verify. Exercised
    # directly against `canonical_urls` (the fetched-set path this collision is about) — the
    # answer-extraction regex has its own, unrelated blind spot for a bracketed literal (`]` ends a
    # candidate match so a markdown label's own closing bracket never bleeds into a URL), which this
    # test is not about.
    from oraclous_harness_runtime_service.domain.link_provenance import canonical_urls

    canon = canonical_urls(["http://[::1]:80/x", "http://[::1:80]/x"])
    assert canon == {"http://[::1]/x", "http://[::1:80]/x"}


async def test_a_punycode_fetch_matches_the_same_host_written_in_unicode() -> None:
    # LOW-7: a bare `.lower()` folds ASCII case but not IDNA — a fetched URL in punycode and the
    # same host written in Unicode in the answer must compare equal, or an honest citation spends a
    # correction over encoding rather than provenance.
    result = _check("[Source](https://münchen.de/a)", ["https://xn--mnchen-3ya.de/a"])
    assert result.unverified == []


async def test_the_default_port_is_folded() -> None:
    # LOW-8: `https://example.org:443/a` and `https://example.org/a` are the same origin. Search
    # connectors routinely hand back the ported form; left un-folded that costs an honest citation
    # a correction.
    result = _check("[Source](https://example.org:443/a)", ["https://example.org/a"])
    assert result.unverified == []


async def test_percent_escape_case_is_folded() -> None:
    # LOW-8: `%2f` and `%2F` name the same octet (RFC 3986). A fetched URL and the model's own prose
    # disagree about the case constantly.
    result = _check("[Source](https://example.org/a%2f)", ["https://example.org/a%2F"])
    assert result.unverified == []


# --- #944 review round 3: two of round 2's own fixes reopened what they had just closed -----------


async def test_an_over_length_url_is_reported_unverified_never_a_verified_prefix() -> None:
    # HIGH-A: round 2 bounded the regex ITSELF at 2048 characters. A URL longer than the cap then
    # matched only a PREFIX of what the reader would actually click — `_trim` shaved that prefix,
    # and the checker verified a STRING THAT WAS NEVER THE DESTINATION. Concretely: this userinfo-
    # phishing URL's first 18 characters equal a URL the run genuinely fetched, so the truncated
    # match read as VERIFIED — reopening the exact userinfo hole the test above (userinfo never
    # verifies) closes, only now returning an AFFIRMATIVE "verified" instead of no signal at all.
    # The fix must report the FULL, untruncated string, unverified.
    fetched = ["https://arxiv.org"]
    smuggled = "https://arxiv.org" + ("." * 2041) + "@evil.example/pwn"
    result = _check(f"[Source]({smuggled})", fetched)
    assert result.verified == []
    assert result.unverified == [smuggled]


async def test_a_userinfo_host_collision_within_the_cap_still_verifies_when_genuinely_fetched() -> (
    None
):
    # The companion property to the test above: an ordinary, WITHIN-CAP URL must still verify
    # normally — the fix must not turn every long-ish URL unverified, only ones the module cannot
    # safely canonicalise (over the cap, or userinfo-bearing).
    result = _check(f"[Source]({_REAL})", [_REAL])
    assert result.unverified == []
    assert result.verified == [_REAL]


async def test_an_ss_domain_does_not_collide_with_its_eszett_lookalike() -> None:
    # HIGH-B: the stdlib `idna` codec is IDNA2003 + nameprep, not the UTS-46 (WHATWG) folding a
    # browser actually performs. The two disagree on the German eszett: the stdlib codec folds
    # `faß.de` to the ASCII string `fass.de`, IDENTICAL to the unrelated real domain `fass.de`,
    # while a browser resolves `faß.de` to a DIFFERENT registrable domain, `xn--fa-hia.de`. Any real
    # target containing "ss" (`businessinsider.com`, `press.*`, `assets.*`) has such a colliding
    # pre-image, so a URL on an attacker's `faß.de` compared equal to a legitimately fetched
    # `fass.de` and reported VERIFIED. Fetching the real ASCII `fass.de` must never verify prose
    # that cites the attacker's `faß.de`.
    result = _check("[Source](https://faß.de/x)", ["https://fass.de/x"])
    assert result.unverified == ["https://faß.de/x"]
    assert result.verified == []


async def test_a_bracketed_ipv6_link_is_reported_unverified_never_dropped() -> None:
    # MEDIUM-G: the answer-extraction regex excludes `]` (so a markdown label's own bracket never
    # bleeds into a URL target), which means a bracketed IPv6 literal used to match only up to the
    # unterminated `[`, fail to parse, and VANISH from `extract_answer_urls` entirely — the answer
    # shipped with a clickable anchor and NO warning at all, worse than reporting it unverified.
    result = _check("[Source](https://[2606:4700::1]/evil)", [])
    assert result.unverified == ["https://[2606:4700::1]/evil"]
    assert result.verified == []


async def test_a_bracketed_ipv6_link_verifies_against_the_same_literal_fetched() -> None:
    # The companion property: a bracketed IPv6 literal the run genuinely fetched must still verify,
    # not merely fail to vanish.
    ipv6 = "https://[2606:4700::1]/status"
    result = _check(f"[Source]({ipv6})", [ipv6])
    assert result.unverified == []
    assert result.verified == [ipv6]


async def test_a_trailing_dot_host_matches_the_same_host_without_one() -> None:
    # LOW-J: `example.org.` names the DNS root the same as `example.org` — the same host. Left
    # un-stripped, a fetched URL using either form reads as a mismatch against an honest citation
    # using the other and costs the member a correction over punctuation, not provenance. The dot
    # sits right after the HOST label, before the path — not sentence punctuation `_trim` would
    # shave off the end of the URL, which is why the pinned bare-URL/paren tests above don't already
    # cover this.
    result = _check("[Source](https://example.org./a)", ["https://example.org/a"])
    assert result.unverified == []


# =================================================================================================
# #975 — cite-by-reference: the two pure functions the acceptance pass is built from.
#
# #944's check is gated on the member declaring tools, so a tool-less `linker` member — whose whole
# job is attaching sources — ships URLs composed from training data, unflagged. The owner ruled
# (2026-09-09) the strongest pattern: the model cites NUMBERED ENTRIES from a source registry the
# platform holds (`[S1]`, `[S2]`, …), and the platform prints the real URL. A fabricated link is
# then impossible by construction. The #944 raw-URL check stays as the backstop, but its consequence
# hardens: an unverified raw URL is STRIPPED from the shipped answer, never rendered.
#
# Two pure functions carry that (same no-I/O posture as `check_answer_links`, same reason):
#
#   expand_source_markers(text, registry) -> (text, unknown)
#     `registry` is the ordered list of URLs the run really fetched; entry n is `registry[n-1]`.
#     Every well-formed marker `[Sn]` naming a registered, gate-passing entry becomes the inline
#     link `[Sn](<url>)` — the label is the marker text (platform-authored, never page-derived:
#     S5), and the target is written in CommonMark angle-bracket form so `(`, `)` and `[` inside a
#     URL can never open or close a markdown construct (S1). A marker naming NO usable entry — out
#     of range, or an entry `_canonical` refuses at expansion time (S1) — is REMOVED from the text
#     and its number reported in `unknown` (1-based, first-seen order, deduplicated). The loop uses
#     `unknown` to spend a correction turn on the ORIGINAL draft; what ships is this function's
#     output, so no literal `[Sn]` ever reaches a reader.
#
#   strip_unverified_links(text, unverified) -> text
#     `unverified` is `LinkCheckResult.unverified` — the answer's URLs AS WRITTEN. Every occurrence
#     of each one is removed: a markdown link that gets stripped loses the WHOLE link, label
#     included (`[Source](https://fab)` → nothing) — issue #991 (owner ruling, 2026-09-09) made S4's
#     rule (a label carrying a URL takes the whole link with it, `[https://fab](https://fab)` →
#     nothing) the general rule for every strip decision, superseding the earlier T1 pin that kept
#     an ordinary label as plain text. A bare URL (no markdown link) is still removed in place.
#     Matching is SPAN-based over `extract_answer_urls`'s own spans, never `str.replace` (T7:
#     stripping `…/a` must not damage `…/ab`). Runs to a fixpoint; idempotent.
#
# Both are imported function-locally (`.claude/rules/tests-seam-imports.md`) — neither name exists
# until the `[impl]` lands, so every test below is RED on `ImportError`, and each also carries an
# assertion that fails on BEHAVIOUR alone once the names exist (T13).
# =================================================================================================

_REG_A = "https://arstechnica.com/ai/2026/09/model-costs-fall-again/"
_REG_B = "https://www.theverge.com/2026/9/8/agents-pricing"
_WIKI = "https://en.wikipedia.org/wiki/Mercury_(planet)"


def _expand(text: str, registry: Any) -> Any:
    from oraclous_harness_runtime_service.domain.link_provenance import expand_source_markers

    return expand_source_markers(text, registry)


def _strip(text: str, unverified: Any) -> Any:
    from oraclous_harness_runtime_service.domain.link_provenance import strip_unverified_links

    return strip_unverified_links(text, unverified)


# --- expand: the substitution itself --------------------------------------------------------------


async def test_a_marker_expands_to_an_inline_link_on_the_registered_url() -> None:
    text, unknown = _expand("Token costs fell again [S1].", [_REG_A])
    assert text == f"Token costs fell again [S1](<{_REG_A}>)."
    assert unknown == []


async def test_the_target_is_in_angle_bracket_form_so_a_paren_in_the_url_cannot_close_it() -> None:
    # S1: `[S1](https://…/Mercury_(planet))` is ambiguous markdown — the URL's own `)` can end the
    # link early. The CommonMark angle-bracket destination `<…>` has no such ambiguity, so a
    # registry entry carrying `(`, `)` or `[` renders as exactly the address the run fetched.
    text, unknown = _expand("Mercury is the smallest planet [S1].", [_WIKI])
    assert text == f"Mercury is the smallest planet [S1](<{_WIKI}>)."
    assert unknown == []


async def test_the_label_is_the_marker_text_never_page_derived_content() -> None:
    # S5: the label a reader clicks is authored by the platform. Nothing from the fetched page —
    # not its title, not its own anchor text — is ever placed there, so page content cannot dress
    # the link. The number is the whole label.
    text, _ = _expand("[S2]", [_REG_A, _REG_B])
    assert text == f"[S2](<{_REG_B}>)"
    assert _REG_A not in text


async def test_the_number_is_the_entry_position_plus_one() -> None:
    text, unknown = _expand("[S1] then [S2]", [_REG_A, _REG_B])
    assert text == f"[S1](<{_REG_A}>) then [S2](<{_REG_B}>)"
    assert unknown == []


async def test_the_same_marker_cited_twice_expands_both_times() -> None:
    text, unknown = _expand("[S1] … and again [S1]", [_REG_A])
    assert text == f"[S1](<{_REG_A}>) … and again [S1](<{_REG_A}>)"
    assert unknown == []


# --- expand: a marker that names no usable entry --------------------------------------------------


async def test_an_out_of_range_marker_is_removed_and_reported() -> None:
    # The model cited a number it was never shown. The number goes to `unknown` so the loop can
    # spend a correction on the draft; the marker itself does not survive into the shipped text
    # (the T3 invariant: no literal `[Sn]` ever reaches a reader).
    text, unknown = _expand("Costs fell [S3].", [_REG_A, _REG_B])
    assert unknown == [3]
    assert "[S3]" not in text
    assert "Costs fell" in text


async def test_a_marker_against_an_empty_registry_is_unknown() -> None:
    text, unknown = _expand("See [S1].", [])
    assert unknown == [1]
    assert "[S1]" not in text


async def test_unknown_markers_are_reported_first_seen_deduplicated() -> None:
    _, unknown = _expand("[S9] and [S9] and [S8] and [S1]", [_REG_A])
    assert unknown == [9, 8]


async def test_a_registry_entry_the_gate_refuses_expands_as_unknown() -> None:
    # S1: the registration gate should never have admitted a userinfo-bearing entry, but the
    # expansion re-checks with `_canonical` rather than trusting the list it was handed —
    # `prior_fetched_urls` is caller-supplied, and a refused entry must not be printed as a real
    # link with the platform's own provenance stamp on it.
    phishing = "https://arxiv.org@evil.example/paper"
    text, unknown = _expand("Read [S1] and [S2].", [phishing, _REG_A])
    assert unknown == [1]
    assert phishing not in text
    assert text == f"Read  and [S2](<{_REG_A}>)."


async def test_an_over_length_registry_entry_expands_as_unknown() -> None:
    oversize = "https://example.org/" + ("a" * 2048)
    text, unknown = _expand("[S1]", [oversize])
    assert unknown == [1]
    assert oversize not in text


# --- expand: the T6 edge matrix -------------------------------------------------------------------


async def test_digits_are_greedy_so_S12_is_entry_twelve_not_entry_one_followed_by_2() -> None:
    registry = [f"https://example.org/{i}" for i in range(1, 13)]
    text, unknown = _expand("[S12]", registry)
    assert text == "[S12](<https://example.org/12>)"
    assert unknown == []


async def test_S12_against_a_one_entry_registry_is_unknown_twelve_not_S1_plus_a_digit() -> None:
    text, unknown = _expand("[S12]", [_REG_A])
    assert unknown == [12]
    assert _REG_A not in text


async def test_adjacent_markers_never_form_a_reference_style_link() -> None:
    # In markdown `[S1][S2]` is a REFERENCE link (label S1, reference S2). Expanded one at a time
    # into inline links, each keeps its own target and the pair cannot collapse into one anchor.
    text, unknown = _expand("[S1][S2]", [_REG_A, _REG_B])
    assert text == f"[S1](<{_REG_A}>)[S2](<{_REG_B}>)"
    assert unknown == []


@pytest.mark.parametrize("marker", ["[s1]", "[S007]", "[S0]", "[[S1]]", "[S 1]", "[S1 ]", "[S]"])
async def test_a_malformed_marker_is_left_alone_and_not_reported(marker: str) -> None:
    # The protocol is exact: an upper-case S, one or more digits with no leading zero, in one pair
    # of brackets. Anything else is ordinary text the member wrote — not expanded, not an offence.
    text, unknown = _expand(f"see {marker} here", [_REG_A])
    assert text == f"see {marker} here"
    assert unknown == []


async def test_a_marker_inside_a_url_span_is_left_alone() -> None:
    # `[` is a legal URL character and the extraction regex admits it, so `https://x/a[S1` is one
    # URL candidate as far as the raw-URL check is concerned. Expanding inside it would rewrite an
    # address; the raw-URL pass will judge the whole span instead.
    raw = "https://example.org/path[S1]/more"
    text, unknown = _expand(f"see {raw}", [_REG_A])
    assert text == f"see {raw}"
    assert unknown == []


async def test_a_marker_inside_a_fenced_code_block_is_left_alone() -> None:
    fenced = "```python\nrefs = ['[S1]']\n```"
    text, unknown = _expand(fenced, [_REG_A])
    assert text == fenced
    assert unknown == []


async def test_a_marker_inside_an_existing_link_label_expands_to_the_bare_url() -> None:
    # `[Report [S1]](https://example.org/r)` — nesting an inline link inside a label is not valid
    # markdown, so the marker becomes the bare address instead. The raw-URL pass then sees a
    # registry URL it will verify, and the outer link is judged on its own target.
    text, unknown = _expand("[Report [S1]](https://example.org/r)", [_REG_A])
    assert text == f"[Report {_REG_A}](https://example.org/r)"
    assert unknown == []


async def test_expansion_is_idempotent() -> None:
    # The expanded form `[S1](<url>)` contains the bytes `[S1]` — running the pass again must not
    # turn it into `[S1](<url>)(<url>)`. What is already a link's own label is not a marker.
    once, _ = _expand("Costs fell [S1] and [S9].", [_REG_A])
    twice, unknown = _expand(once, [_REG_A])
    assert twice == once
    assert unknown == []


async def test_empty_text_and_empty_registry_are_a_no_op() -> None:
    assert _expand("", []) == ("", [])
    assert _expand("no markers here", []) == ("no markers here", [])


async def test_the_registry_may_be_any_collection_of_strings() -> None:
    # The loop's accumulator is a list; a caller may hand a tuple. Order is what numbers it.
    text, _ = _expand("[S2]", (_REG_A, _REG_B))
    assert text == f"[S2](<{_REG_B}>)"


# --- expand: a JSON answer must stay parseable (T8) -----------------------------------------------


async def test_a_marker_inside_a_json_string_expands_and_the_document_still_parses() -> None:
    # A member with a declared output contract answers with a JSON document, and the engine parses
    # the declared keys OUT OF THE REWRITTEN TEXT (`team_run.py`). A substitution that breaks a
    # string escape breaks every downstream consumer of that member's output.
    import json

    text, unknown = _expand('{"summary": "Costs fell [S1].\\n\\nMore below."}', [_REG_A])
    parsed = json.loads(text)
    assert parsed["summary"] == f"Costs fell [S1](<{_REG_A}>).\n\nMore below."
    assert unknown == []


# --- strip: the T7 matrix -------------------------------------------------------------------------


async def test_a_markdown_link_with_an_unverified_target_is_removed_entirely() -> None:
    # Issue #991 (owner ruling, 2026-09-09): a stripped link takes its label with it, not just its
    # target. `[Source](https://fab)` → nothing — a bare "Source" left behind reads as dead text,
    # and the console showed it as its own bullet, duplicating the real story (run 57eb8029).
    result = _strip(f"Costs fell. [Source]({_FABRICATED})", [_FABRICATED])
    assert result == "Costs fell. "


async def test_a_link_whose_label_is_itself_the_url_is_removed_entirely() -> None:
    # S4: the label is what the reader SEES. `[https://fab](https://fab)` stripped to `https://fab`
    # would leave the fabricated address on the screen as text — and the console linkifies bare
    # URLs, so it would be an anchor again. Label and target both go.
    result = _strip(f"Sources:\n- [{_FABRICATED}]({_FABRICATED})\n- [Ars]({_REG_A})", [_FABRICATED])
    assert _FABRICATED not in result
    assert f"[Ars]({_REG_A})" in result


async def test_a_link_whose_label_merely_contains_a_url_is_removed_entirely() -> None:
    # The same rule when the URL is embedded in prose inside the label, and when the label carries
    # a DIFFERENT url than the target — any http(s) URL in the label takes the whole link with it.
    other = "https://www.forbes.com/sites/nobody/2026/01/01/invented/"
    result = _strip(f"see [details at {other} today]({_FABRICATED}) now", [_FABRICATED, other])
    assert _FABRICATED not in result
    assert other not in result
    assert result == "see  now"


async def test_a_bare_unverified_url_is_removed_in_place() -> None:
    result = _strip(f"See {_FABRICATED} for the breakdown.", [_FABRICATED])
    assert _FABRICATED not in result
    assert "See" in result
    assert "for the breakdown." in result


async def test_sentence_punctuation_after_a_stripped_bare_url_survives() -> None:
    result = _strip(f"The breakdown is at {_FABRICATED}.", [_FABRICATED])
    assert result == "The breakdown is at ."


async def test_stripping_is_span_based_so_a_prefix_never_damages_a_longer_verified_url() -> None:
    # T7, the prefix trap: `str.replace("https://example.org/a", "")` would turn the VERIFIED
    # `https://example.org/ab` into `b`. Only a URL whose own extracted span equals an unverified
    # entry is touched. Issue #991: the stripped `[A]` link takes its label with it too, so only the
    # verified `[B]` link survives.
    short = "https://example.org/a"
    longer = "https://example.org/ab"
    result = _strip(f"[A]({short}) and [B]({longer})", [short])
    assert result == f" and [B]({longer})"


async def test_a_verified_url_survives_byte_identical_however_it_was_written() -> None:
    # Issue #991: the unverified `[Okta]` link is removed WHOLE, label included; the verified
    # `[Ars]` link is untouched.
    written = "https://WWW.ArsTechnica.com/ai/2026/09/model-costs-fall-again/#cost-table"
    answer = f"[Ars]({written}) and [Okta]({_FABRICATED})"
    result = _strip(answer, [_FABRICATED])
    assert result == f"[Ars]({written}) and "


async def test_the_same_url_written_as_markdown_and_bare_is_removed_in_both_places() -> None:
    # Issue #991: the markdown link's label ("Source") goes with its target; the bare URL is removed
    # in place as before.
    result = _strip(f"[Source]({_FABRICATED}) … and again at {_FABRICATED}", [_FABRICATED])
    assert _FABRICATED not in result
    assert "Source" not in result
    assert result == " … and again at "


async def test_an_angle_bracket_target_is_stripped_the_same_way() -> None:
    # A model may write the CommonMark form itself. The regex stops at `<`/`>`, so the extracted
    # span is the bare address; the strip still has to take the whole `[label](<url>)` construct —
    # issue #991: label included.
    result = _strip(f"Costs fell [Source](<{_FABRICATED}>).", [_FABRICATED])
    assert result == "Costs fell ."


async def test_stripping_runs_to_a_fixpoint_on_nested_link_shapes() -> None:
    # `[[Source](https://fab)](https://fab)`: removing the inner link exposes an outer one that was
    # not a well-formed link before. One pass is not enough; the result must contain the URL
    # nowhere and be stable under a second pass.
    nested = f"[[Source]({_FABRICATED})]({_FABRICATED})"
    once = _strip(f"Costs fell. {nested}", [_FABRICATED])
    assert _FABRICATED not in once
    assert _strip(once, [_FABRICATED]) == once


async def test_stripping_is_idempotent() -> None:
    answer = f"[A]({_FABRICATED}) and [B]({_REG_A}) and {_FABRICATED}"
    once = _strip(answer, [_FABRICATED])
    assert _strip(once, [_FABRICATED]) == once


async def test_an_empty_unverified_list_returns_the_text_byte_identical() -> None:
    answer = f"[A]({_FABRICATED}) and {_REG_A}."
    assert _strip(answer, []) == answer


async def test_a_url_not_present_in_the_text_strips_nothing() -> None:
    answer = f"[A]({_REG_A})."
    assert _strip(answer, [_FABRICATED]) == answer


async def test_a_gate_refused_url_is_stripped_too() -> None:
    # `_canonical` refuses userinfo and over-length shapes, so `check_answer_links` reports them
    # unverified as written. The strip must remove exactly that as-written string — these are the
    # shapes a reader must never be handed. Issue #991: the label ("paper") goes with it.
    phishing = "https://arxiv.org@evil.example/paper"
    result = _strip(f"[paper]({phishing}) is the source.", [phishing])
    assert result == " is the source."


async def test_stripping_inside_a_json_string_leaves_the_document_parseable() -> None:
    import json

    answer = (
        f'{{"summary": "Prices fell.\\n\\nSources:\\n- [Ars]({_REG_A})\\n- [Okta]({_FABRICATED})"}}'
    )
    result = _strip(answer, [_FABRICATED])
    parsed = json.loads(result)
    assert _FABRICATED not in parsed["summary"]
    assert f"[Ars]({_REG_A})" in parsed["summary"]
    # Issue #991: the whole `[Okta](...)` link goes, label included — the line keeps its leading
    # bullet dash but ends with nothing after it.
    assert parsed["summary"].endswith("- ")
    assert "Okta" not in parsed["summary"]


# --- the two passes composed: expansion output is never a strip target ----------------------------


async def test_an_expanded_link_survives_a_strip_that_names_only_the_fabricated_url() -> None:
    # The loop runs expand, then `check_answer_links` on what remains, then strip on the
    # unverified survivors. A registry URL inserted by expansion is by construction fetched, so it
    # is never in `unverified` — and the strip must leave the angle-bracket link intact. Issue #991:
    # the fabricated `[Okta]` link is removed WHOLE, label included.
    expanded, _ = _expand(f"Costs fell [S1]. [Okta]({_FABRICATED})", [_REG_A])
    result = _strip(expanded, [_FABRICATED])
    assert result == f"Costs fell [S1](<{_REG_A}>). "


# =================================================================================================
# PR #977 security review (review 5155075040) — fold-in findings, ruled on by the orchestrator
# 2026-09-09. Every case below is reproduced live by `scratchpad/poc_975.py` before this commit:
# each of the eight shapes in B1 leaks `evil.example` straight through
# `expand -> check_answer_links -> strip` TODAY, because `strip_unverified_links` judges a markdown
# link by comparing its raw, as-written TARGET STRING against tokens `extract_answer_urls` produced
# from an INDEPENDENT scan of the same text — not by the URL(s) actually sitting inside that link's
# own target/label spans. Two scans of the same text can tokenise it differently (a title, a
# trailing space, a second link immediately adjacent), and whenever they do, the strip's string
# equality silently fails and the "unverified" link ships intact.
#
# The fix judges a link by the URL(s) INSIDE it, canonicalised, against the canonical form of every
# `unverified` token — never by string equality of the as-written target — and `extract_answer_urls`
# / `check_answer_links` tokenise `[label](target)` spans first, so an adjacent link's own target is
# never smeared into its neighbour's.
# =================================================================================================


def _pipeline(text: str, registry: Any) -> str:
    # The exact sequence the loop runs at answer acceptance (`domain/loop/tool_use.py`): expand
    # markers, check what remains against the registry, strip the survivors. `poc_975.py`'s
    # `pipeline()` helper, reproduced here as a fixture rather than imported — a test file owns its
    # own fixtures, and importing a one-off scratchpad script into a permanent suite is a mistake in
    # the other direction.
    from oraclous_harness_runtime_service.domain.link_provenance import (
        check_answer_links,
        expand_source_markers,
        strip_unverified_links,
    )

    expanded, _ = expand_source_markers(text, registry)
    check = check_answer_links(expanded, registry)
    return strip_unverified_links(expanded, check.unverified) if check.unverified else expanded


_B1_REG = ["https://real.example/report"]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('See [Source](https://evil.example/report "ref").', id="commonmark_title"),
        pytest.param("See [Source](https://evil.example/report ).", id="trailing_space_in_target"),
        pytest.param("See [Source](https://evil.example/report)x", id="non_punct_after_paren"),
        pytest.param("See [Source](https://evil.example/report)— more", id="em_dash_after_paren"),
        pytest.param(
            "[A](https://evil.example/a)[B](https://evil.example/b)", id="two_adjacent_links"
        ),
        pytest.param(
            "[A](https://evil.example/a),[B](https://evil.example/b)",
            id="comma_separated_adjacent_links",
        ),
        pytest.param(
            'See [Source](<https://evil.example/report> "t").', id="angle_target_and_title"
        ),
    ],
)
async def test_b1_a_leaking_link_shape_never_survives_the_pipeline(text: str) -> None:
    # security review 5155075040, PoC Area 1/4 — every one of these seven shapes leaked
    # "https://evil.example/..." through the pipeline today; none of them is exotic, all seven are
    # ordinary markdown a real model writes.
    out = _pipeline(text, _B1_REG)
    assert "evil" not in out


async def test_b1_the_ok_control_still_strips_a_plain_unverified_link() -> None:
    # The companion property: an ordinary, non-adversarial unverified link must still be stripped —
    # the fix must not turn every link into a survivor, only the ones today's string-equality check
    # cannot see. Issue #991: the whole link goes, label included.
    out = _pipeline("See [Source](https://evil.example/report).", _B1_REG)
    assert "evil" not in out
    assert out == "See ."


async def test_b1_check_answer_links_reports_the_real_target_for_adjacent_links() -> None:
    # The extraction-tokenisation half of B1: today `extract_answer_urls`'s own trimming reports
    # the first adjacent link's "target" as `https://evil.example/a)[B` — a string that names no
    # real link at all — because `_URL` is greedy across the second link's own opening bracket.
    # `check_answer_links` must report the link's REAL target, `https://evil.example/a`, which is
    # only possible if `[label](target)` spans are tokenised first, before any bare-URL scan runs.
    from oraclous_harness_runtime_service.domain.link_provenance import check_answer_links

    text = "[A](https://evil.example/a)[B](https://evil.example/b)"
    result = check_answer_links(text, _B1_REG)
    assert "https://evil.example/a" in result.unverified
    assert "https://evil.example/b" in result.unverified
    assert not any(u.startswith("https://evil.example/a)[B") for u in result.unverified)


# --- property test: a hand-built grammar matrix (hypothesis is not a repo dependency) -------------
#
# `uv run python -c "import hypothesis"` fails with ModuleNotFoundError in both the repo root venv
# and this service's own venv — not a dependency here. This is the hand-built matrix the plan calls
# for instead: 30 compositions of {markdown link, title, trailing space, angle-bracket target,
# adjacency, punctuation, a marker, a fenced code block} run through the same
# `expand -> check_answer_links -> strip` pipeline. Every composition's fabricated material sits on
# `evil.example`, never the registry's `real.example`, so the invariant collapses to one substring
# check per composition: the host must never survive.

_PROP_REG = ["https://real.example/report"]
_PROP_TARGETS = [
    "https://{h}/x",
    'https://{h}/x "t"',
    "https://{h}/x ",
    "<https://{h}/x>",
    '<https://{h}/x> "t"',
]
_PROP_SUFFIXES = [
    "",
    ".",
    ")",
    "— more",
    "[Next](https://{h}/y)",
    ",[Next](https://{h}/y)",
]
_PROP_PREFIXES = [
    "",
    "Plain sentence. ",
    "[S1] ",
    "```\nrefs = ['[S1]']\n```\n",
]


def _compose(i: int) -> str:
    n_targets, n_suffixes = len(_PROP_TARGETS), len(_PROP_SUFFIXES)
    target = _PROP_TARGETS[i % n_targets].format(h="evil.example")
    suffix = _PROP_SUFFIXES[(i // n_targets) % n_suffixes].format(h="evil.example")
    prefix = _PROP_PREFIXES[(i // (n_targets * n_suffixes)) % len(_PROP_PREFIXES)]
    return f"{prefix}[Ev]({target}){suffix}"


_PROP_COMPOSITIONS = [_compose(i) for i in range(30)]


@pytest.mark.parametrize(
    "text", _PROP_COMPOSITIONS, ids=[f"composition_{i}" for i in range(len(_PROP_COMPOSITIONS))]
)
async def test_property_no_fabricated_host_survives_any_grammar_composition(text: str) -> None:
    out = _pipeline(text, _PROP_REG)
    assert "evil.example" not in out


# --- M1: a fabricated URL as the LABEL of a genuinely verified link -------------------------------


async def test_a_fabricated_label_on_a_verified_link_is_dropped_whole_given_the_registry() -> None:
    # security review 5155075040 (M1), PoC Area 1/4: `[https://evil.example/x](https://real.
    # example/report)` ships TODAY — the TARGET verifies, so `unverified` is empty and
    # `strip_unverified_links` is never even asked about this link. But the LABEL is what a reader
    # SEES, and it names an address the run never fetched. Telling "fabricated" from "verified" for
    # a LABEL url needs the registry (unlike a target, which `check_answer_links` already
    # classified) — hence the new keyword-only `fetched` parameter.
    from oraclous_harness_runtime_service.domain.link_provenance import strip_unverified_links

    text = "[https://evil.example/x](https://real.example/report)"
    result = strip_unverified_links(text, [], fetched=["https://real.example/report"])
    assert "evil" not in result
    assert result == ""


# --- m2: a registry entry carrying `<`, `>`, whitespace or a control character is refused ---------

_POISON_ENTRIES = [
    pytest.param("https://real.example/>)[click](https://evil.example/phish)", id="angle_bracket"),
    pytest.param("https://real.example/a\tb", id="tab"),
    pytest.param("https://real.example/a\nb", id="newline"),
    pytest.param("https://real.example/a\x00b", id="null_byte"),
    pytest.param("https://real.example/a b", id="embedded_space"),
]


@pytest.mark.parametrize("entry", _POISON_ENTRIES)
async def test_a_poisoned_registry_entry_is_never_canonicalised(entry: str) -> None:
    # security review 5155075040 (m2), PoC Area 4: `_canonical` accepts a `>`-bearing entry today.
    # Wrapped in `expand_source_markers`'s CommonMark angle-bracket target, the entry's OWN `>`
    # closes the `<...>` early, and the rest of the string — `)[click](https://evil.example/
    # phish)` — is read back as fresh, live markdown. A registry entry is never something a reader
    # sees raw; it must fail the same canonicalisation gate every other source does.
    from oraclous_harness_runtime_service.domain.link_provenance import canonical_urls

    assert canonical_urls([entry]) == set()


@pytest.mark.parametrize("entry", _POISON_ENTRIES)
async def test_a_marker_citing_a_poisoned_registry_entry_is_unknown(entry: str) -> None:
    text, unknown = _expand("Read [S1].", [entry])
    assert unknown == [1]
    assert "evil" not in text
    assert "click" not in text


async def test_check_answer_links_never_verifies_against_a_poisoned_fetched_entry() -> None:
    # security review 5155075040 (m2): a poisoned registry entry must never let
    # `check_answer_links` VERIFY anything against it, and the evil URL it smuggles in must still
    # come back UNVERIFIED — neither silently dropped nor misclassified as fetched. Embedded via a
    # plain (non-angle) `(...)` target, this poison string is not one link: B1's tokenisation
    # (pinned by test_b1_check_answer_links_reports_the_real_target_for_adjacent_links, which
    # requires the exact opposite — an adjacent link's own target must never bleed past its own
    # close) correctly reads it as TWO adjacent, independently well-formed links, not one — the
    # poisoned entry's own truncated target, and the `evil.example` URL immediately following it.
    # That is a finer partition than the original single-entry shape expected, not a leak: both
    # fragments still land in `unverified`, and neither ever reaches `verified`, so the m2 property
    # this test exists to pin — a poisoned fetched entry verifies nothing, and the URL it smuggles
    # in is never lost — holds regardless of how many pieces the tokeniser reports it as
    # (backend-implementer, PR #977 comment 5603856386).
    poison = "https://real.example/>)[click](https://evil.example/phish)"
    result = _check(f"[Source]({poison})", [poison])
    assert result.unverified == ["https://real.example/>", "https://evil.example/phish"]
    assert result.verified == []


# --- m1: expansion stays near-linear on attacker-supplied interleaving ----------------------------


async def test_expansion_of_8000_interleaved_url_and_marker_pairs_stays_near_linear() -> None:
    # security review 5155075040 (m1), PoC Area 7: `_inside_any` bisects `url_spans`/`fence_spans`
    # LINEARLY per marker today — O(n*m) in the number of markers times the number of URL/fence
    # spans — measured at 1.79s for 8000 interleaved "https://a.example/p [S1]" pairs. A page a
    # member's tool reads is attacker-supplied text with no bound on how many URLs and markers it
    # can interleave; this has to stay well under the same 0.5s bound `_trim`'s own bounded-time
    # test already holds itself to.
    text = " ".join("https://a.example/p [S1]" for _ in range(8000))
    reg = ["https://a.example/p"]
    started = time.monotonic()
    expanded, unknown = _expand(text, reg)
    assert time.monotonic() - started < 0.5
    assert unknown == []
    assert expanded.count("[S1](<https://a.example/p>)") == 8000


# =================================================================================================
# Security round 2 (review 5156136217, PoC `scratchpad/poc_975_round2.py`) — two fold-ins ruled by
# the orchestrator into THIS PR: N2 (a regression 18d0b85's B1 rewrite introduced) and N1 (a new
# bypass class in the same two functions). Both root causes are the same "a target that is not
# literally `http(s)://` is treated as nothing to this check" gap, in `_iter_written_urls` (N2) and
# `_strip_pass` (N1) — the exact two functions 18d0b85 rewrote for B1.
# =================================================================================================

_N2_REG = ["https://real.example/report"]


# --- N2 (MAJOR, regression): a non-http(s) target hides the literal-scheme URL inside it ----------
#
# At bd350da (before 18d0b85's B1 rewrite) `check_answer_links` reported `https://evil.example/r`
# unverified for this shape. 18d0b85's link-first tokenisation now consumes the whole
# `[label](target)` span, finds the target does not start with `http(s)://`, and yields nothing from
# the link branch — so the bare-URL scan, which used to see this address because nothing had
# consumed it yet, never runs on it either. Not a valid CommonMark link (the target is not a URL),
# so it renders as literal text carrying the fabricated `https://` address in full, and a working
# anchor under GFM autolink literals.


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("[S](x https://evil.example/r)", id="leading_word_then_url"),
        pytest.param("[S](<x> https://evil.example/r)", id="angle_non_url_target_then_url"),
        pytest.param('[S](x https://evil.example/r "t")', id="leading_word_then_url_and_title"),
    ],
)
async def test_n2_a_non_http_target_no_longer_hides_the_literal_url_it_carries(text: str) -> None:
    # Every one of these leaked `evil.example` through `expand -> check_answer_links -> strip` at
    # 18c387d. `check_answer_links` must go back to reporting the real address (as it did at
    # bd350da), and the shipped pipeline must never carry it.
    result = _check(text, _N2_REG)
    assert result.unverified == ["https://evil.example/r"]
    out = _pipeline(text, _N2_REG)
    assert "evil" not in out


async def test_n2_a_url_named_only_in_the_label_of_a_non_url_target_is_never_extracted() -> None:
    # The sibling composition N2's fix must NOT start reaching into: a label is never scanned for
    # the answer's OWN url list (`_iter_written_urls`'s own docstring) — a fabricated LABEL is
    # `strip_unverified_links`'s `fetched=`/M1 concern, not extraction's. `check_answer_links` must
    # keep reporting nothing here even once N2's fix touches the same target-scanning code path, and
    # the fabricated address must still never ship, via M1's existing label defence.
    text = "[https://evil.example/r](x)"
    result = _check(text, _N2_REG)
    assert result.unverified == []
    assert result.verified == []
    from oraclous_harness_runtime_service.domain.link_provenance import strip_unverified_links

    assert "evil" not in strip_unverified_links(text, result.unverified, fetched=_N2_REG)


# --- regression guard: the #944 detection shapes already pinned above must keep firing ------------


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(f"See {_FABRICATED} for the breakdown.", id="bare"),
        pytest.param(
            f"Token costs fell sharply this quarter. [Source]({_FABRICATED})", id="markdown"
        ),
        pytest.param(
            f'{{"summary": "Prices fell.\\n\\nSources:\\n- '
            f'[Ars]({_REAL})\\n- [Okta]({_FABRICATED})"}}',
            id="json_escaped",
        ),
        pytest.param(f"The breakdown (see {_FABRICATED}) is clear.", id="parenthesised"),
    ],
)
async def test_n2_regression_guard_the_944_shapes_still_report_the_literal_url(
    answer: str,
) -> None:
    # N2 was a regression IN this exact family of shapes — a tokeniser rewrite hid a literal-scheme
    # URL that used to be reported. Pinned as one parametrized list, not four separate tests
    # scattered through this file, so a future tokeniser change can never silently narrow coverage
    # back down to only the shapes someone remembered to keep testing.
    result = _check(answer, [_REAL])
    assert _FABRICATED in result.unverified


# --- N1 (MAJOR, strip-only): a target `_canonical` never accepts still ships as a working anchor --
#
# Not a regression — bd350da shipped these too. But the invariant is "the shipped text contains only
# registry URLs", and `strip_unverified_links`'s `fetched=` path (the loop's real acceptance path)
# still passes every one of these through untouched today: a scheme-relative target, a
# malformed-scheme target a browser's URL parser normalises back to `https://`, and a
# backslash-as-slash target. Fixed by failing closed: with `fetched` given, a link whose target has
# no canonical form IN THE REGISTRY is dropped whole (label included — same S4 reasoning B1/M1
# already established for this file).

_N1_REG = ["https://real.example/report"]


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("//evil.example/phish", id="protocol_relative"),
        pytest.param("https:/evil.example/x", id="single_slash_scheme"),
        pytest.param("https:evil.example/x", id="no_slash_scheme"),
        pytest.param("\\\\evil.example\\phish", id="backslash_protocol_relative"),
        pytest.param("javascript:alert(1)", id="javascript_scheme"),
    ],
)
async def test_n1_an_unusable_target_is_dropped_whole_when_fetched_is_given(target: str) -> None:
    from oraclous_harness_runtime_service.domain.link_provenance import strip_unverified_links

    text = f"[Source]({target})"
    result = strip_unverified_links(text, [], fetched=_N1_REG)
    assert result == ""


async def test_n1_a_mailto_target_is_also_dropped_fail_closed_given_fetched() -> None:
    # #944's own scheme allow-list ruling already keeps `mailto:` out of what this check reads as a
    # link — it is not a clickable-to-article scheme, so there is no ruling under which it belongs
    # in a citation registry. N1's fail-closed rule extends that ruling to the SHIPPED path: a
    # `mailto:` target has no canonical http(s) form, so `fetched=` drops it whole, exactly like
    # any other target the registry can never contain.
    from oraclous_harness_runtime_service.domain.link_provenance import strip_unverified_links

    text = "[Source](mailto:team@example.org)"
    result = strip_unverified_links(text, [], fetched=_N1_REG)
    assert result == ""


async def test_n1_a_registered_target_survives_the_fetched_fail_closed_rule_byte_identical() -> (
    None
):
    from oraclous_harness_runtime_service.domain.link_provenance import strip_unverified_links

    text = f"[Source]({_N1_REG[0]})"
    result = strip_unverified_links(text, [], fetched=_N1_REG)
    assert result == text


async def test_n1_the_two_argument_path_leaves_a_scheme_relative_link_untouched() -> None:
    # T1's contract, unchanged: with no `fetched` given, `strip_unverified_links` only ever removes
    # what `unverified` names. Flagging semantics for a target this check does not recognise as a
    # link belong to `check_answer_links`, not to a fail-closed rule that exists only on the
    # `fetched=` path the loop actually ships through.
    from oraclous_harness_runtime_service.domain.link_provenance import strip_unverified_links

    text = "[Source](//evil.example/phish)"
    assert strip_unverified_links(text, []) == text


# =================================================================================================
# Security round 3 (review 5156579514, PoC `scratchpad/poc_975_round3.py`) — three NEW MAJOR
# fold-ins, all three sharing one root cause with N1/N2: the shipped path still decides what
# survives by membership in `unverified` (a scan of the PRE-strip text) rather than by membership in
# the REGISTRY on the text that actually ships. Orchestrator ruling: `fetched` becomes an explicit
# registry-mode switch (`Collection[str] | None = None`, `registry_mode = fetched is not None`) —
# `fetched=None` stays the T1 two-argument path, byte-identical; `fetched=[]` (an EMPTY but
# non-None registry) is now registry mode too, which is exactly N5. After this round: no further
# security round; these tests are the verification the PoC calls for.
# =================================================================================================

_N3_REAL = "https://real.example/report"
_N3_REG = [_N3_REAL]


def _pipeline_registry(text: str, registry: Any) -> str:
    # Unlike `_pipeline` above (T1/B1, two-argument strip), this mirrors the LOOP'S OWN acceptance
    # path: `strip_unverified_links` is always called with `fetched=` there (`tool_use.py`), never
    # bare. N3/N4/N5 are all bypasses of that `fetched=` path specifically.
    from oraclous_harness_runtime_service.domain.link_provenance import (
        check_answer_links,
        expand_source_markers,
        strip_unverified_links,
    )

    expanded, _ = expand_source_markers(text, registry)
    check = check_answer_links(expanded, registry)
    return strip_unverified_links(expanded, check.unverified, fetched=registry)


# --- N3 (MAJOR): the strip's own rewrite composes a URL nothing ever checked ----------------------
#
# Pass 1 strips a link down to a label that carries no `://` of its own (S4 lets a plain-text label
# like `https:` survive as text); that label then lands flush against the very next character in the
# text, and pass 2's bare-URL scan judges the NEWLY COMPOSED span against `unverified_raw`/
# `unverified_canonical` — sets computed from the text BEFORE pass 1 ever ran, which never contained
# this string. The four inputs below are the review's own reproduction, each `LEAK`ing at 42dd983.

_N3_CASES = [
    pytest.param(
        "[https:](https://evil.example/b)//evil.example/x",
        "evil",
        id="label_https_colon_then_scheme_relative_tail",
    ),
    pytest.param(
        "https:/[](https://evil.example/b)/evil.example/x",
        "evil",
        id="prefix_https_slash_then_empty_label_link_then_tail",
    ),
    pytest.param(
        "[https](https://evil.example/b)://evil.example/x",
        "evil",
        id="label_https_then_scheme_tail",
    ),
    pytest.param(
        f"{_N3_REAL} [](https://evil.example/b)/phish",
        "phish",
        id="real_prefix_then_empty_label_link_then_phish_tail",
    ),
]


@pytest.mark.parametrize("text,needle", _N3_CASES)
async def test_n3_the_strips_own_rewrite_never_composes_a_url_nothing_checked(
    text: str, needle: str
) -> None:
    out = _pipeline_registry(text, _N3_REG)
    assert needle not in out


@pytest.mark.parametrize("text,needle", _N3_CASES)
async def test_n3_holds_even_when_the_registry_is_explicitly_empty(text: str, needle: str) -> None:
    # `fetched=[]` is registry mode with nothing in it (the tool-less `linker` with no seeds) — N3's
    # composed-URL bug has to stay fixed there too, not only once a real registry entry happens to
    # be present. This is the review's own N3d probe, generalised to all four inputs.
    out = _pipeline_registry(text, [])
    assert needle not in out


# --- N4 (MAJOR): a verified target hides every other URL in the same `(...)` from both scans ------
#
# `_iter_written_urls`'s link branch returns as soon as the trimmed target itself is a URL — it
# never scans `target_raw` for a SECOND url sitting after that real target (a title, or plain text
# after an angle-bracket destination that is not valid CommonMark and so renders as literal text a
# GFM autolinker still turns into a working anchor).

_N4_REG = [_N3_REAL]
_N4_CASES = [
    pytest.param(
        f"[S](<{_N3_REAL}> https://evil.example/r)",
        id="angle_real_target_then_bare_evil_url",
    ),
    pytest.param(
        f'[S]({_N3_REAL} "https://evil.example/r")',
        id="real_target_then_evil_url_in_double_quoted_title",
    ),
    pytest.param(
        f"[S](<{_N3_REAL}> 'https://evil.example/r')",
        id="angle_real_target_then_evil_url_in_single_quoted_title",
    ),
    pytest.param(
        f'[S](<{_N3_REAL}> "see https://evil.example/r")',
        id="angle_real_target_then_evil_url_inside_title_prose",
    ),
]


@pytest.mark.parametrize("text", _N4_CASES)
async def test_n4_check_answer_links_reports_the_url_hidden_beside_a_verified_target(
    text: str,
) -> None:
    from oraclous_harness_runtime_service.domain.link_provenance import check_answer_links

    result = check_answer_links(text, _N4_REG)
    assert "https://evil.example/r" in result.unverified


@pytest.mark.parametrize("text", _N4_CASES)
async def test_n4_the_pipeline_never_ships_the_url_hidden_beside_a_verified_target(
    text: str,
) -> None:
    out = _pipeline_registry(text, _N4_REG)
    assert "evil" not in out


# --- N5 (MAJOR): N1's fail-closed rule is keyed on `fetched` TRUTHINESS, so an EMPTY registry -----
# switches it off — exactly the tool-less member #975 exists to protect ---------------------------


async def test_n5_an_empty_but_non_none_registry_still_fails_closed_on_an_unusable_target() -> None:
    from oraclous_harness_runtime_service.domain.link_provenance import strip_unverified_links

    text = "[Source](//evil.example/phish)"
    assert strip_unverified_links(text, [], fetched=[]) == ""


async def test_n5_fetched_none_stays_the_two_argument_path_left_untouched() -> None:
    # The control: `fetched=None` is NOT registry mode (the T1 two-argument contract), so it must
    # stay exactly as unaffected by N5's fix as it already is by N1's.
    from oraclous_harness_runtime_service.domain.link_provenance import strip_unverified_links

    text = "[Source](//evil.example/phish)"
    assert strip_unverified_links(text, [], fetched=None) == text
