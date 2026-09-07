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


# --- criterion 7: the provenance set is matched the same way ----------------------------------


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
