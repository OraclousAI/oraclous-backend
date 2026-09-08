"""#961 ruling 3 — a restricted search that found nothing says so in a key the run can act on.

Ruled 2026-09-08: a list of sites that genuinely cannot be honoured FINISHES the run, marked
incomplete, with a plain sentence saying the named sites had nothing. Not a failure. #580's shape
(a retrieval that found nothing degrades to a flagged PARTIAL rather than crashing) is reused
deliberately, so no new failure mode is invented.

#951 already writes a sentence into the result for this case, and that sentence is for the MODEL —
it stops the member re-running the identical search. What did not exist is anything the RUNTIME can
read, so the run itself still settled as an ordinary success and a person never learned that their
addresses came back empty.

This adds that: one reserved key on the result, set only on a search that WAS restricted and came
back with nothing. The harness pops it before the model ever sees it, and believes it only from a
first-party search row — the #781 posture, applied from the start rather than after an incident,
because a key a model can read is a key a model can learn to write.

**The trap this file also closes.** Every connector unit test in this repo builds its executor as
``SomeConnector({"id": "x"})``, which declares no input shape, so the type check that runs BEFORE
the connector never runs in these tests at all. #951 shipped 54 green tests over a call the live
stack refused. Nothing here changes the tool's declared input shape — the ``sites`` argument and its
schema are #951's, untouched — and the two assertions at the bottom pin that, so a later change
that quietly widens the schema fails here instead of on the deployed stack.

RED-by-design until the ``[impl]`` lands.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.web_research import (
    WebResearchConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000007a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000007c5")

#: The reserved key the harness reads. Named for what it MEANS, not for the status it produces —
#: the runtime decides what an empty restricted search does to a run, and the connector only
#: reports what happened.
EMPTY_KEY = "sites_yielded_nothing"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
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


def _hits(*results: dict) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": list(results)})

    return handler


async def test_a_restricted_search_with_no_hits_flags_it() -> None:
    """The signal the runtime needs. Without it the run settles as an ordinary success and nobody
    learns that the sites they named carried nothing matching."""
    res = await _connector(_hits()).execute(
        {"operation": "search", "query": "night trains", "sites": ["theverge.com"]}, _ctx()
    )

    assert res.success is True
    assert res.data[EMPTY_KEY] is True


async def test_a_restricted_search_that_found_something_flags_nothing() -> None:
    """The ordinary restricted search. A flag on a successful search would degrade every run that
    used a site list, which is the opposite of what ruling 3 asks for."""
    res = await _connector(
        _hits({"title": "T", "url": "https://theverge.com/a", "content": "c"})
    ).execute({"operation": "search", "query": "trains", "sites": ["theverge.com"]}, _ctx())

    assert EMPTY_KEY not in res.data


async def test_an_unrestricted_search_with_no_hits_flags_nothing() -> None:
    """The scope guard, at the connector. An empty result from a whole-web search is an ordinary
    empty result and always has been — this issue is only about a restriction that came up dry."""
    res = await _connector(_hits()).execute(
        {"operation": "search", "query": "asdfghjkl qwertyuiop"}, _ctx()
    )

    assert EMPTY_KEY not in res.data


async def test_the_sentence_for_the_model_is_still_there_and_still_names_the_sites() -> None:
    """#951's half is unchanged. The flag is for the runtime; the sentence is for the member, so it
    proceeds instead of re-running the identical search, and it names the addresses actually used
    because a wrong-but-plausible address can only ever be SHOWN, never detected."""
    res = await _connector(_hits()).execute(
        {"operation": "search", "query": "trains", "sites": ["theverge.com", "bbc.co.uk"]}, _ctx()
    )

    assert "theverge.com" in res.data["note"]
    assert "bbc.co.uk" in res.data["note"]
    assert res.data["searched_sites"] == ["theverge.com", "bbc.co.uk"]


async def test_the_flag_rides_in_the_data_the_registry_persists() -> None:
    """``metadata`` is dropped at the execution boundary — ``tool_execution_service`` finalises with
    ``output_data=result.data`` — so a flag left only in metadata would reach neither the harness
    nor the run's trace. #951 learned this the same way about ``searched_sites``."""
    res = await _connector(_hits()).execute(
        {"operation": "search", "query": "trains", "sites": ["theverge.com"]}, _ctx()
    )

    assert EMPTY_KEY in json.loads(json.dumps(res.data, default=str))


# ── the shared cleaner: one rule, three services ────────────────────────────────────────────────


def test_the_registry_uses_the_kernel_cleaner_rather_than_its_own_copy() -> None:
    """Asserted by IDENTITY, not by behaviour.

    A behavioural test ("both turn a pasted link into a bare hostname") passes just as happily
    against two implementations that agree today — which is the state #946's two-cleaning-passes
    defect was in the day before it broke. The harness now compares the model's ``sites`` argument
    against the person's list, so a second copy of this rule would put a disagreement between two
    services rather than two functions.
    """
    from oraclous_capability_registry_service.domain.connectors import search_providers
    from oraclous_ohm.sites import InvalidSiteError, normalise_sites

    assert search_providers.normalise_sites is normalise_sites
    assert search_providers.InvalidSiteError is InvalidSiteError


def test_the_declared_input_shape_is_unchanged() -> None:
    """The trap named above, pinned.

    Unit tests build the connector with no declared input shape, so the validation layer in front
    of it never runs here. Nothing in this change touches that shape — ``sites`` still accepts a
    list or a string, exactly as #951 shipped it — and this fails if a later change forgets that
    the deployed stack checks what these tests cannot.
    """
    from oraclous_capability_registry_service.domain.plugins.builtin import WebResearchPlugin

    sites = WebResearchPlugin.INPUT_SCHEMA["properties"]["sites"]
    assert sites == {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]}
