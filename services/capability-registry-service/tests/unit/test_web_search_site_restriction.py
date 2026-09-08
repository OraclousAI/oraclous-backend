"""Unit (#951 T4/T5/T6): a web search can be restricted to named websites.

A person converts a team into an app and fills in its "News Websites" box. Today that value reaches
the member as one line of prose and dies there: ``core/web-research.search`` has no argument for
"only these sites", so the digest comes back from wherever. #946 removed the argument models kept
mistaking for one (``provider``, the search VENDOR); this builds the control that was missing.

Three things are pinned here, and one of them is a security property rather than a nicety:

* **T4 — the restriction reaches the vendor.** A ``sites`` argument on the ``search`` path lands in
  the outgoing request body as the vendor's own ``include_domains``. No sites means byte-for-byte
  today's unrestricted request, so every existing caller is unchanged.
* **T5 — an address is stripped to a bare hostname before it is sent.** A live probe on 2026-09-07
  established that the vendor SILENTLY IGNORES a full URL: ``https://theverge.com/tech`` returns an
  ordinary 200 whose results are completely unrestricted, with no error and no warning. So
  normalising is a correctness requirement. Passing a pasted address straight through would rebuild
  the exact silent no-op #951 exists to remove.
* **T6 — the model is told what the argument is for.** The flat hint map behind the older path can
  express only a type, which is precisely why ``provider`` reached a model as a bare unexplained
  string. ``search`` declares a real JSON schema instead, with a sentence per argument.

**The run reports the hostnames it actually searched.** No mechanical check can tell ``bbc.com``
from ``bbc.co.uk`` — both return real pages — so the honest answer to a wrong-but-plausible address
is to show the person what was used. The reported value is the CLEANED hostname, never the raw text
typed, or a mismatch between the two stays hidden. It lands in ``data`` (not only ``metadata``)
because the registry's execution boundary keeps ``result.data`` alone; a value left in ``metadata``
reaches neither the member nor the run's step trace.

Ruled 2026-09-08: a bare string is ACCEPTED and split on commas rather than ignored. A model that
sends ``"theverge.com, bbc.co.uk"`` is asking for a restriction in the wrong container, and
dropping it silently is the bug. Accepting is safe only because the run reports back what it used.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.search_providers import (
    TavilySearchProvider,
)
from oraclous_capability_registry_service.domain.connectors.web_research import (
    WebResearchConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.plugins.builtin import (
    WebResearchPlugin,
    WebSearchToolPlugin,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000951")
_USER = uuid.UUID("00000000-0000-0000-0000-000000000952")

#: The cap on how many sites one search may name. The vendor's own ceiling is 300 (400 entries came
#: back a 400 on the live probe); ours sits well below it because a person filling in a box names a
#: handful, and a runaway list is a mistake worth refusing near where it was made.
_EXPECTED_SITE_CAP = 20


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    # pinned so an ambient value cannot flip the `provider` this file asserts on
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        execution_id=uuid.uuid4(),
        credentials={"api_key": {"api_key": "tvly-secret"}},
    )


def _connector(handler: Callable[[httpx.Request], httpx.Response]) -> WebResearchConnector:
    ex = WebResearchConnector({"id": "x"})
    ex.transport = httpx.MockTransport(handler)
    return ex


def _recording_handler(seen: dict, hits: list[dict] | None = None) -> Callable:
    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"results": hits if hits is not None else []})

    return handler


def _validating_connector(
    plugin: Any, handler: Callable[[httpx.Request], httpx.Response]
) -> WebResearchConnector:
    """A connector built WITH its declared input schema, so the check that runs ahead of it runs.

    The plain ``WebResearchConnector({"id": "x"})`` every other test uses declares no schema, and
    ``InternalTool._schema_problem`` validates nothing when there is none — so those tests reach the
    connector directly and never cross the layer that refused a bare string on the live stack.
    """
    ex = WebResearchConnector({"id": "x", "spec": {"input_schema": plugin.INPUT_SCHEMA}})
    ex.transport = httpx.MockTransport(handler)
    return ex


def _search_operation(plugin: Any) -> dict:
    return next(op for op in plugin.CAPABILITIES if op["name"] == "search")


# --- T5: the cleaner (the seam this issue adds) --------------------------------------------------
#
# Imported function-locally: `normalise_sites` does not exist until the `[impl]` lands, and a
# module-level import would abort collection for the whole suite rather than reddening these tests
# alone (.claude/rules/tests-seam-imports.md).


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # the three forms a person actually pastes all land on one value
        ("theverge.com", ["theverge.com"]),
        ("www.theverge.com", ["theverge.com"]),
        ("https://theverge.com/tech", ["theverge.com"]),
        # the live probe's silent-no-op case, in every dress it arrives in
        ("http://www.bbc.co.uk/news?section=tech#top", ["bbc.co.uk"]),
        ("HTTPS://TheVerge.COM/Tech", ["theverge.com"]),
        # trailing punctuation and a port a person copied along with the address
        ("theverge.com/", ["theverge.com"]),
        ("theverge.com.", ["theverge.com"]),
        ("theverge.com:8080", ["theverge.com"]),
        ("  theverge.com  ", ["theverge.com"]),
        # a multi-label host keeps every label it needs
        ("news.bbc.co.uk", ["news.bbc.co.uk"]),
    ],
)
def test_one_address_is_stripped_to_a_bare_hostname(raw: str, expected: list[str]) -> None:
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        normalise_sites,
    )

    assert normalise_sites([raw]) == expected


def test_nothing_asked_for_means_nothing_restricted() -> None:
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        normalise_sites,
    )

    assert normalise_sites(None) == []
    assert normalise_sites([]) == []
    assert normalise_sites("") == []
    assert normalise_sites("   ") == []
    assert normalise_sites(["", "   "]) == []


def test_a_bare_string_is_split_on_commas_rather_than_ignored() -> None:
    """Ruled 2026-09-08. A model sending the list in one string is asking for a restriction in the
    wrong container. Ignoring it is the silent no-op this issue exists to remove; splitting it is
    an unambiguous reading, and the run reports back what it used, so a misreading is visible."""
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        normalise_sites,
    )

    assert normalise_sites("theverge.com, bbc.co.uk") == ["theverge.com", "bbc.co.uk"]
    assert normalise_sites("theverge.com") == ["theverge.com"]
    assert normalise_sites("https://theverge.com/tech, www.bbc.co.uk") == [
        "theverge.com",
        "bbc.co.uk",
    ]
    assert normalise_sites("theverge.com,,  ,bbc.co.uk") == ["theverge.com", "bbc.co.uk"]


def test_a_list_entry_carrying_several_addresses_is_split_too() -> None:
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        normalise_sites,
    )

    assert normalise_sites(["theverge.com, bbc.co.uk", "arstechnica.com"]) == [
        "theverge.com",
        "bbc.co.uk",
        "arstechnica.com",
    ]


def test_the_same_site_named_twice_is_sent_once_in_the_order_given() -> None:
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        normalise_sites,
    )

    assert normalise_sites(["www.theverge.com", "bbc.co.uk", "https://theverge.com/x"]) == [
        "theverge.com",
        "bbc.co.uk",
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "not a hostname!!",
        "localhost",  # single-label: no valid public suffix, and the vendor 400s it
        "127.0.0.1",  # an address literal is not a domain
        "::1",
        "a..b.com",  # an empty label
        "-bad.com",
        "bad-.com",
        "x_y.com",  # underscores are not legal in a hostname
        "file:///etc/passwd",
        "javascript:alert(1)",
        "theverge.com/tech and also bbc.co.uk",
    ],
)
def test_a_value_that_is_not_an_address_is_refused_and_named(bad: str) -> None:
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        InvalidSiteError,
        normalise_sites,
    )

    with pytest.raises(InvalidSiteError) as exc:
        normalise_sites([bad])
    # the member is told WHICH value was wrong, so it can fix that one rather than guess
    assert bad.strip() in str(exc.value)


def test_a_non_string_entry_is_refused_rather_than_coerced() -> None:
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        InvalidSiteError,
        normalise_sites,
    )

    for bad in (123, None, {"host": "theverge.com"}, ["theverge.com"]):
        with pytest.raises(InvalidSiteError):
            normalise_sites([bad])


def test_a_value_that_is_neither_a_list_nor_a_string_is_refused() -> None:
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        InvalidSiteError,
        normalise_sites,
    )

    for bad in (42, True, {"sites": ["theverge.com"]}):
        with pytest.raises(InvalidSiteError):
            normalise_sites(bad)


def test_an_over_long_list_is_refused_not_silently_trimmed() -> None:
    """Truncating would drop sites the person named without saying so — the same silent class of
    bug as the ignored URL. The refusal names the cap so the caller can act on it."""
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        InvalidSiteError,
        normalise_sites,
    )

    at_cap = [f"site{n}.example.com" for n in range(_EXPECTED_SITE_CAP)]
    assert len(normalise_sites(at_cap)) == _EXPECTED_SITE_CAP

    with pytest.raises(InvalidSiteError) as exc:
        normalise_sites([*at_cap, "one-too-many.example.com"])
    assert str(_EXPECTED_SITE_CAP) in str(exc.value)


def test_an_oversized_value_is_not_quoted_back_whole() -> None:
    """A refusal names the offending value so the caller can fix THAT one — but the value is
    caller-supplied and unbounded, and the message travels into a run's error text and onto a
    person's screen. Without the bound a 400-character argument becomes a 400-character message."""
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        InvalidSiteError,
        normalise_sites,
    )

    huge = "a" * 400 + "!!"
    with pytest.raises(InvalidSiteError) as exc:
        normalise_sites([huge])
    message = str(exc.value)
    assert huge not in message
    assert len(message) < 250, message
    assert message.startswith("'aaa")  # still names the value, just not all of it


def test_an_absurdly_long_hostname_is_refused() -> None:
    """Five sixty-character labels are each individually legal, so the per-label pattern accepts the
    whole thing — the total-length bound is the only thing that refuses it."""
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        InvalidSiteError,
        normalise_sites,
    )

    too_long = ".".join(["a" * 60] * 5) + ".com"
    assert len(too_long) > 253
    with pytest.raises(InvalidSiteError):
        normalise_sites([too_long])


def test_a_non_ascii_address_is_sent_in_the_form_the_vendor_understands() -> None:
    """A person in Germany types münchen.de. Dropping the conversion flips it from a working
    restriction to "not a website address", which no other test would notice."""
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        normalise_sites,
    )

    assert normalise_sites(["münchen.de"]) == ["xn--mnchen-3ya.de"]
    assert normalise_sites(["https://www.münchen.de/rathaus"]) == ["xn--mnchen-3ya.de"]


def test_a_blank_entry_on_its_own_is_refused_rather_than_reaching_the_vendor() -> None:
    """``normalise_sites`` skips blank parts, so this is the guard's only caller-visible path."""
    from oraclous_capability_registry_service.domain.connectors import search_providers
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        InvalidSiteError,
    )

    with pytest.raises(InvalidSiteError):
        search_providers._hostname_of("")


def test_a_malformed_address_literal_is_refused_rather_than_raising() -> None:
    """An unclosed bracket makes the URL parser itself raise; it must surface as a refusal the
    caller can read, never as an unhandled error."""
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        InvalidSiteError,
        normalise_sites,
    )

    with pytest.raises(InvalidSiteError):
        normalise_sites(["http://[fe80::1"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("www.www.theverge.com", ["theverge.com"]),
        ("https://www.www.bbc.co.uk/news", ["bbc.co.uk"]),
        ("www.www.www.example.com", ["example.com"]),
    ],
)
def test_cleaning_an_address_twice_is_the_same_as_cleaning_it_once(
    raw: str, expected: list[str]
) -> None:
    """The cleaning runs TWICE — once where the connector records what it searched, once at the
    last hop before the vendor. Stripping only one ``www.`` per call made those two passes
    disagree, so the run reported one hostname and searched a different, broader one. Reporting
    honestly what was searched is this feature's whole justification, so the clean has to be a
    fixed point rather than merely tidy."""
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        normalise_sites,
    )

    once = normalise_sites([raw])
    assert once == expected
    assert normalise_sites(once) == once


async def test_the_hostnames_reported_are_the_hostnames_sent() -> None:
    """The same defect where it is visible to a person: a report that disagrees with the request.
    Asserted through the connector, because that is where the two passes meet."""
    seen: dict = {}
    ex = _connector(_recording_handler(seen))
    res = await ex.execute(
        {"operation": "search", "query": "ai news", "sites": ["www.www.theverge.com"]}, _ctx()
    )
    assert res.success
    assert res.data is not None
    assert res.data["searched_sites"] == seen["body"]["include_domains"]
    assert res.data["searched_sites"] == ["theverge.com"]


@pytest.mark.security
@pytest.mark.parametrize(
    ("pasted", "secret"),
    [
        ("https://alice:hunter2@localhost/x", "hunter2"),
        ("https://theverge.com/x?token=SEKRET-abc and more", "SEKRET-abc"),
        ("http://svc:p4ssw0rd@not a host", "p4ssw0rd"),
    ],
)
def test_a_refusal_never_writes_down_a_secret_the_caller_pasted(pasted: str, secret: str) -> None:
    """The REFUSE path is the one that writes the value down: the message becomes the execution
    row's ``error_message`` and is rendered on a person's screen. The accept path was already safe
    — only the hostname is ever sent — so covering only that hid this entirely."""
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        InvalidSiteError,
        normalise_sites,
    )

    with pytest.raises(InvalidSiteError) as exc:
        normalise_sites([pasted])
    assert secret not in str(exc.value)


@pytest.mark.security
async def test_a_pasted_secret_stays_out_of_the_failure_the_caller_reads() -> None:
    """The same threat one layer out, where the value actually reaches a person."""
    ex = _connector(_recording_handler({}))
    res = await ex.execute(
        {
            "operation": "search",
            "query": "ai news",
            "sites": ["https://alice:hunter2@not a host"],
        },
        _ctx(),
    )
    assert not res.success
    assert res.error_type == "INVALID_INPUT"
    assert "hunter2" not in (res.error_message or "")


@pytest.mark.security
async def test_a_vendor_refusal_on_valid_sites_still_never_echoes_its_body() -> None:
    """ADR-008. The sites are well-formed, so our own check passes them and the VENDOR is the one
    that refuses — with a body that names the domains. That body must not reach the caller.

    This is the shape the earlier version of this test could not reach: it used a bad address, so
    the local refusal fired first and the vendor was never asked."""
    ex = _connector(
        lambda _req: httpx.Response(
            400, json={"detail": "All domains in include_domains are invalid: ['theverge.com']"}
        )
    )
    res = await ex.execute({"operation": "search", "query": "q", "sites": ["theverge.com"]}, _ctx())
    assert not res.success
    assert "All domains in include_domains" not in (res.error_message or "")
    assert "PROVIDER" in (res.error_type or "")


@pytest.mark.security
def test_credentials_pasted_into_an_address_never_reach_the_vendor() -> None:
    """A person pasting a URL from their address bar can paste a userinfo prefix with it. Only the
    hostname is sent, so the secret is dropped here rather than travelling to a third party."""
    from oraclous_capability_registry_service.domain.connectors.search_providers import (
        normalise_sites,
    )

    cleaned = normalise_sites(["https://alice:hunter2@theverge.com/tech"])
    assert cleaned == ["theverge.com"]


# --- T4: the restriction reaches the vendor ------------------------------------------------------


async def test_the_named_sites_land_in_the_outgoing_request_body() -> None:
    seen: dict = {}
    hits = await TavilySearchProvider().search(
        "ai news",
        api_key="tvly-secret",
        sites=["theverge.com", "bbc.co.uk"],
        transport=httpx.MockTransport(_recording_handler(seen)),
    )
    assert hits == []
    # `include_domains` is the vendor's own parameter name, confirmed against the live API
    assert seen["body"]["include_domains"] == ["theverge.com", "bbc.co.uk"]


async def test_the_vendor_is_handed_hostnames_even_when_addresses_were_supplied() -> None:
    """The whole point of T5: a full URL reaches the vendor as a bare hostname, because a URL is
    accepted with a 200 and then silently ignored."""
    seen: dict = {}
    await TavilySearchProvider().search(
        "ai news",
        api_key="tvly-secret",
        sites=["https://www.theverge.com/tech"],
        transport=httpx.MockTransport(_recording_handler(seen)),
    )
    assert seen["body"]["include_domains"] == ["theverge.com"]


@pytest.mark.parametrize("sites", [None, []])
async def test_an_unrestricted_search_sends_exactly_the_request_it_sends_today(
    sites: object,
) -> None:
    """Not "an empty list is harmless" — the key is ABSENT, so an existing caller's request is
    byte-for-byte what it was before this argument existed."""
    seen: dict = {}
    await TavilySearchProvider().search(
        "ai news",
        api_key="tvly-secret",
        sites=sites,  # type: ignore[arg-type]
        transport=httpx.MockTransport(_recording_handler(seen)),
    )
    assert "include_domains" not in seen["body"]
    assert set(seen["body"]) == {"api_key", "query", "max_results", "search_depth"}


async def test_the_search_operation_passes_the_named_sites_through() -> None:
    seen: dict = {}
    ex = _connector(_recording_handler(seen))
    res = await ex.execute(
        {"operation": "search", "query": "ai news", "sites": ["www.theverge.com"]}, _ctx()
    )
    assert res.success
    assert seen["body"]["include_domains"] == ["theverge.com"]


async def test_the_standard_search_tool_accepts_the_same_argument() -> None:
    from oraclous_capability_registry_service.domain.connectors.standard_tools import (
        WebSearchConnector,
    )

    seen: dict = {}
    ex = WebSearchConnector({"id": "x"})
    ex.transport = httpx.MockTransport(_recording_handler(seen))
    res = await ex.execute({"query": "ai news", "sites": ["https://bbc.co.uk/news"]}, _ctx())
    assert res.success
    assert seen["body"]["include_domains"] == ["bbc.co.uk"]
    # the output half of the same promise: the tool's own description tells a member to report the
    # addresses the RESULT says were searched, so the result has to say them here too
    assert res.data is not None
    assert res.data["searched_sites"] == ["bbc.co.uk"]


async def test_a_search_with_no_sites_is_unchanged_end_to_end() -> None:
    seen: dict = {}
    ex = _connector(_recording_handler(seen, hits=[{"title": "T", "url": "https://a.test/1"}]))
    res = await ex.execute({"operation": "search", "query": "ai news"}, _ctx())
    assert res.success
    assert "include_domains" not in seen["body"]
    assert res.data is not None
    assert "searched_sites" not in res.data
    assert "note" not in res.data


async def test_a_bad_address_is_refused_before_any_request_is_made() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": []})

    ex = _connector(handler)
    res = await ex.execute(
        {"operation": "search", "query": "ai news", "sites": ["BBC News"]}, _ctx()
    )
    assert not res.success
    assert res.error_type == "INVALID_INPUT"
    assert "BBC News" in (res.error_message or "")
    assert calls["n"] == 0


@pytest.mark.security
async def test_credentials_pasted_into_an_address_reach_neither_the_vendor_nor_the_caller() -> None:
    """The wire half of the same threat: it is not enough that the cleaner drops the secret, the
    request that leaves the process and every field the caller reads must be free of it too."""
    seen: dict = {}
    ex = _connector(_recording_handler(seen))
    res = await ex.execute(
        {
            "operation": "search",
            "query": "ai news",
            "sites": ["https://alice:hunter2@theverge.com/tech"],
        },
        _ctx(),
    )
    assert res.success
    assert seen["body"]["include_domains"] == ["theverge.com"]
    assert "hunter2" not in json.dumps(seen["body"])
    rendered = json.dumps({"data": res.data, "metadata": res.metadata, "err": res.error_message})
    assert "hunter2" not in rendered
    assert "alice" not in rendered


async def test_an_over_long_list_is_refused_before_any_request_is_made() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": []})

    ex = _connector(handler)
    res = await ex.execute(
        {
            "operation": "search",
            "query": "ai news",
            "sites": [f"site{n}.example.com" for n in range(_EXPECTED_SITE_CAP + 1)],
        },
        _ctx(),
    )
    assert not res.success
    assert res.error_type == "INVALID_INPUT"
    assert str(_EXPECTED_SITE_CAP) in (res.error_message or "")
    assert calls["n"] == 0


# --- T4/D4b: the run says which hostnames it actually searched ------------------------------------


async def test_the_result_the_member_reads_names_the_hostnames_used() -> None:
    """Without this the member cannot truthfully say where its answer came from, and a person who
    typed a plausible-but-wrong address has nothing to correct."""
    seen: dict = {}
    ex = _connector(_recording_handler(seen, hits=[{"title": "T", "url": "https://a.test/1"}]))
    res = await ex.execute(
        {"operation": "search", "query": "ai news", "sites": ["theverge.com", "bbc.co.uk"]},
        _ctx(),
    )
    assert res.success
    assert res.data is not None
    assert res.data["searched_sites"] == ["theverge.com", "bbc.co.uk"]


async def test_the_reported_hostnames_are_the_cleaned_ones_never_the_raw_text() -> None:
    """Reporting back what was typed would hide the very mismatch this exists to surface."""
    seen: dict = {}
    ex = _connector(_recording_handler(seen))
    res = await ex.execute(
        {"operation": "search", "query": "ai news", "sites": ["HTTPS://WWW.TheVerge.com/tech"]},
        _ctx(),
    )
    assert res.success
    assert res.data is not None
    assert res.data["searched_sites"] == ["theverge.com"]
    assert res.data["searched_sites"] == seen["body"]["include_domains"]


async def test_the_reported_hostnames_are_carried_on_both_result_surfaces() -> None:
    """Named for what it checks: the list is on ``data`` AND on ``metadata``. It does not reach the
    persistence seam, so it cannot prove the step trace by itself — but ``data`` is the carrier that
    gets there, because ``tool_execution_service`` finalises with ``output_data=result.data`` and
    never touches ``result.metadata``. A list left only in metadata reaches nobody."""
    seen: dict = {}
    ex = _connector(_recording_handler(seen))
    res = await ex.execute(
        {"operation": "search", "query": "ai news", "sites": ["theverge.com"]}, _ctx()
    )
    assert res.success
    assert res.data is not None
    assert res.data["searched_sites"] == ["theverge.com"]
    assert res.metadata["searched_sites"] == ["theverge.com"]
    assert res.metadata["provider"] == "tavily"


async def test_finding_nothing_on_the_named_sites_is_reported_as_absence_not_failure() -> None:
    """An empty restricted result is data-absence (ADR-021 degrade-not-crash), so the run says so
    in a sentence the member can act on rather than failing or looking broken."""
    seen: dict = {}
    ex = _connector(_recording_handler(seen, hits=[]))
    res = await ex.execute(
        {"operation": "search", "query": "quantum tea kettle", "sites": ["theverge.com"]}, _ctx()
    )
    assert res.success
    assert res.data is not None
    note = res.data["note"]
    assert "theverge.com" in note
    # a sentence, not a status token — the same bar #946 set for a failed run's text
    assert note.strip().endswith(".")
    assert "ERROR" not in note.upper()


@pytest.mark.security
async def test_an_empty_restricted_result_never_forges_the_data_absence_flag() -> None:
    """``data_absent`` is the knowledge-retriever's reserved key: #781 made the runtime BELIEVE it
    only from a trusted retrieval binding, because a forged one buys a softer citation terminal.
    Web search must not emit it at all."""
    seen: dict = {}
    ex = _connector(_recording_handler(seen, hits=[]))
    res = await ex.execute({"operation": "search", "query": "q", "sites": ["theverge.com"]}, _ctx())
    assert res.data is not None
    assert "data_absent" not in res.data
    # the restriction really was applied, so this is not passing for the reason it did before
    # the argument existed at all
    assert res.data["searched_sites"] == ["theverge.com"]


@pytest.mark.security
async def test_a_refusal_still_never_echoes_the_vendors_own_body() -> None:
    """The vendor 400s an invalid domain with a body naming it. ADR-008: an upstream body is never
    surfaced. Our own refusal comes first, so the vendor is not even asked."""
    ex = _connector(
        lambda _req: httpx.Response(
            400, json={"detail": "All domains in include_domains are invalid: [...]"}
        )
    )
    res = await ex.execute(
        {"operation": "search", "query": "q", "sites": ["not a hostname!!"]}, _ctx()
    )
    assert not res.success
    # the refusal is OURS: it names the value locally and is typed as bad input, rather than
    # arriving as whatever the vendor happened to answer
    assert res.error_type == "INVALID_INPUT"
    assert "not a hostname!!" in (res.error_message or "")


@pytest.mark.security
async def test_the_api_key_is_still_absent_from_everything_the_caller_sees() -> None:
    seen: dict = {}
    ex = _connector(_recording_handler(seen))
    res = await ex.execute(
        {"operation": "search", "query": "ai news", "sites": ["theverge.com"]}, _ctx()
    )
    rendered = json.dumps({"data": res.data, "metadata": res.metadata, "err": res.error_message})
    assert "tvly-secret" not in rendered
    # the key is absent from a result that really carries the new reporting, not from one that
    # simply ignored the argument
    assert res.data is not None
    assert res.data["searched_sites"] == ["theverge.com"]


# --- T6: the model is told what the argument is for ----------------------------------------------


def test_the_search_operation_declares_a_real_schema_for_the_model() -> None:
    """The flat hint map can carry only a type — no description, no ``items``. That is exactly how
    ``provider`` reached a model as a bare unexplained string (#946). ``parameters_schema`` is
    passed to the model UNCHANGED by the harness (#698 D1), so the sentences land."""
    schema = _search_operation(WebResearchPlugin)["parameters_schema"]
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"query", "max_results", "sites"}


def test_naming_sites_stays_optional() -> None:
    """Only ``query`` is required. If ``sites`` were required, every model call would have to name
    websites and an ordinary unrestricted search would become impossible — a change no other test
    in this file would notice, because they all reach the connector directly and skip the schema."""
    for plugin in (WebResearchPlugin, WebSearchToolPlugin):
        assert _search_operation(plugin)["parameters_schema"]["required"] == ["query"]


def test_every_argument_the_model_is_offered_carries_a_description() -> None:
    for plugin in (WebResearchPlugin, WebSearchToolPlugin):
        schema = _search_operation(plugin)["parameters_schema"]
        for name, prop in schema["properties"].items():
            assert prop.get("description", "").strip(), f"{plugin.__name__}.{name}"


def test_the_sites_argument_is_typed_as_a_list_of_strings() -> None:
    for plugin in (WebResearchPlugin, WebSearchToolPlugin):
        sites = _search_operation(plugin)["parameters_schema"]["properties"]["sites"]
        assert sites["type"] == "array"
        assert sites["items"] == {"type": "string"}


def test_the_sites_description_asks_for_an_address_not_a_publication_name() -> None:
    """Ruling 3: turning "BBC News" into a hostname is a guess, and a plausible wrong guess is
    indistinguishable from a right one. The description must ask for the thing that needs no
    guessing, and show what one looks like."""
    for plugin in (WebResearchPlugin, WebSearchToolPlugin):
        text = _search_operation(plugin)["parameters_schema"]["properties"]["sites"]["description"]
        # a bar a bare type name or a restated argument name cannot clear
        assert len(text) > 80, text
        lowered = text.lower()
        assert "hostname" in lowered or "address" in lowered
        # an example is what stops a model inventing a shape
        assert "bbc.co.uk" in lowered or "theverge.com" in lowered


def test_the_standard_search_tool_offers_the_same_arguments() -> None:
    web_research = set(_search_operation(WebResearchPlugin)["parameters_schema"]["properties"])
    standard = set(_search_operation(WebSearchToolPlugin)["parameters_schema"]["properties"])
    assert web_research == standard


def test_the_hint_map_and_the_schema_do_not_drift_apart() -> None:
    """Both are kept: the schema is what a model sees, the hint map is the descriptor's older
    surface. Two lists of the same argument names in one file drift, so they are pinned together."""
    for plugin in (WebResearchPlugin, WebSearchToolPlugin):
        op = _search_operation(plugin)
        assert set(op["parameters"]) == set(op["parameters_schema"]["properties"])


def test_the_vendor_argument_stays_out_of_what_the_model_is_offered() -> None:
    """#946's whole point, re-pinned here because this issue rewrites the same descriptor entry."""
    for plugin in (WebResearchPlugin, WebSearchToolPlugin):
        assert "provider" not in _search_operation(plugin)["parameters_schema"]["properties"]


async def test_several_sites_in_one_string_survive_the_check_that_runs_before_the_connector() -> (
    None
):
    """The defect the live gateway run found, which 54 green unit tests could not.

    ``input_validation`` enforces a declared top-level ``type`` from the tool's own input schema
    BEFORE ``_execute_internal``, so declaring ``sites`` as ``array`` refused a bare string at that
    boundary — "sites must be a array, got string" — and the comma-splitting never happened. This
    is the only test that RUNS that layer, so it is the only one that would catch the declaration
    narrowing again."""
    for plugin in (WebResearchPlugin, WebSearchToolPlugin):
        seen: dict = {}
        ex = _validating_connector(plugin, _recording_handler(seen))
        res = await ex.execute(
            {"operation": "search", "query": "ai news", "sites": "theverge.com, arstechnica.com"},
            _ctx(),
        )
        assert res.success, res.error_message
        assert seen["body"]["include_domains"] == ["theverge.com", "arstechnica.com"]
        assert res.data is not None
        assert res.data["searched_sites"] == ["theverge.com", "arstechnica.com"]


async def test_a_list_of_sites_still_passes_that_same_check() -> None:
    """The widened declaration must not stop accepting the shape a model is actually asked for."""
    seen: dict = {}
    ex = _validating_connector(WebResearchPlugin, _recording_handler(seen))
    res = await ex.execute(
        {"operation": "search", "query": "ai news", "sites": ["theverge.com"]}, _ctx()
    )
    assert res.success, res.error_message
    assert seen["body"]["include_domains"] == ["theverge.com"]


def test_the_connectors_own_input_schema_lets_a_bare_string_reach_the_connector() -> None:
    """Corrected 2026-09-08 after the live run. This file's earlier version required
    ``INPUT_SCHEMA``'s ``sites`` to be declared ``array``, which is what the argument SHOULD be —
    but ``input_validation`` enforces a declared top-level ``type`` BEFORE the connector runs, so
    that declaration refused a bare string at the boundary with "sites must be a array, got
    string". The comma-splitting ruled on 2026-09-08 was then unreachable, and the caller got an
    ungrammatical message that never says what to send instead.

    So the connector's own input schema declares BOTH accepted shapes. The model-facing
    ``parameters_schema`` still asks for an array — that is what a model should send — and the
    connector, not a type check two layers up, is what handles a string and says why."""
    for plugin in (WebResearchPlugin, WebSearchToolPlugin):
        sites = plugin.INPUT_SCHEMA["properties"]["sites"]
        assert "type" not in sites, sites  # a declared type here refuses the string at the boundary
        assert {"type": "array", "items": {"type": "string"}} in sites["anyOf"]
        assert {"type": "string"} in sites["anyOf"]


def test_the_operations_a_model_can_reach_are_still_the_same_three() -> None:
    assert {op["name"] for op in WebResearchPlugin.CAPABILITIES} == {"search", "fetch", "read"}


def test_fetch_and_read_are_untouched_by_this_change() -> None:
    """``sites`` restricts a SEARCH. The two URL operations keep the flat hint map they have."""
    for name in ("fetch", "read"):
        op = next(o for o in WebResearchPlugin.CAPABILITIES if o["name"] == name)
        assert op["parameters"] == {"url": "str"}
