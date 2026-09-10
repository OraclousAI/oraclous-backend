"""#944 + #975 — an inline link the run never fetched is STRIPPED, end to end through the gateway.

The reported bug, live: a member's free-text answer wrote ``[Source](https://www.okta.com/blog/…)``
and nothing in the pipeline ever read that URL, so the console rendered it as a real anchor and a
person clicked it trusting the run had fetched it (team run ``8ef18ab0``, the ``linker`` role).

#944 shipped a post-hoc check but SHIPPED the bad link anyway, flagged. #975 found the real gap:
the check was gated on the member declaring tools at all, so a tool-less "linker" role — the exact
role the bug report named, whose whole job is attaching sources — was never checked. The owner's
ruling (2026-09-09) hardens the consequence for every member, tools or not: a raw URL the run never
fetched is now STRIPPED from the shipped answer, never rendered, and the run proves the pass ran via
either a ``link_provenance`` GATE step in the member's steps or the URL named in that member's own
``unverified_links``.

This proves both halves on the DEPLOYED stack, through the gateway only, with a real model and a
real web search — no fakes, no injected server-side state, nothing asserted against the database.
The user brings their own model key and their own search key through the public credentials API,
exactly as a person does.

  * A single tool-declaring member (the original #944 shape) is told to cite one real, searched
    source and one link it must not look up. The invented one must never reach the shipped answer.
  * A two-member team — ``researcher`` (has web tools) -> ``linker`` (``tools: []``) — proves the
    fix's actual point: the tool-less ``linker`` is handed the same invented link, planted as BOTH
    the label and the target of a markdown link so it is tempted to reproduce it verbatim, and it
    must be stripped from ``linker``'s answer just the same. It also proves the registry now
    crosses the member boundary (#975 T5): a URL the ``researcher`` really fetched reaches the
    ``linker``'s shipped answer, so a reader gets a real citation instead of nothing.

Requires the harness LIVE and both keys (``scripts/e2e.sh --byom``). Auto-skips otherwise, and a
skip is NOT a pass.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")
_SEARCH_KEY = os.environ.get("TAVILY_API_KEY")
requires_keys = pytest.mark.skipif(
    _MODEL_KEY is None or _SEARCH_KEY is None,
    reason="OPENROUTER_API_KEY / TAVILY_API_KEY unset (real BYOM + real web search)",
)

# The URL a member is told to cite and told not to look up. A real host, an invented path — the
# exact shape of the reported fabrication, and nothing about it can be caught by inspection alone.
_INVENTED = "https://www.okta.com/blog/2023/10/okta-ai-token-costs"

_GATE_NAME = "link_provenance"


def _store(c: httpx.Client, user_id: str, provider: str, key: str, name: str) -> str:
    resp = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": name,
            "provider": provider,
            "cred_type": "api_key",
            "credential": {"api_key": key},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _connect_web_research(c: httpx.Client, credential_id: str) -> None:
    """Give this organisation a configured instance of the web-search tool, through the gateway."""
    catalogue = c.get("/api/v1/capabilities").json()["capabilities"]
    capability = next((x for x in catalogue if x["name"] == "Web Research"), None)
    assert capability is not None, "the registry has no Web Research capability"
    instance = c.post(
        "/api/v1/instances",
        json={"capability_id": capability["id"], "name": "Web Research", "configuration": {}},
    )
    assert instance.status_code in (200, 201), instance.text
    configured = c.post(
        f"/api/v1/instances/{instance.json()['id']}/configure-credentials",
        json={"credential_mappings": {"api_key": credential_id}},
    )
    assert configured.status_code in (200, 201), configured.text


def _shipped(result: dict) -> str:
    """The text a reader actually sees for this member — the parsed ``summary`` (#697's declared-
    key lift) when the model answered with valid JSON, the raw ``output`` otherwise. Checking only
    ``output`` would also work (the URLs land in it either way) but this is what the console shows.
    """
    summary = result.get("summary")
    return str(summary) if summary is not None else str(result.get("output") or "")


def _has_link_gate_step(steps: list[object] | None) -> bool:
    return any(isinstance(s, dict) and s.get("name") == _GATE_NAME for s in steps or [])


def _no_marker_survives(text: str) -> bool:
    """A marker the platform did NOT expand (#980). A correctly expanded citation reads
    ``[Sn](<url>)`` — the pinned label KEEPS the literal ``[Sn]`` text (`expand_source_markers`'s
    label is the marker itself, never page-derived, S5) — so the bare substring ``[S\\d+]`` still
    occurs in a fully-resolved answer and is not evidence anything survived unexpanded. Only a
    marker with no trailing ``(`` is one the acceptance pass left as a literal, naming nothing."""
    return re.search(r"\[S\d+\](?!\()", text) is None


_EXPANDED_MARKER = re.compile(r"\[S\d+\]\(<([^<>]+)>\)")


def _expanded_marker_urls(text: str) -> list[str]:
    """Every target URL of an ``[Sn](<url>)``-shaped link actually shipped — the platform's own
    deterministic expansion of a marker the model cited. Proves the cite-by-reference protocol
    really ran WITHOUT depending on the model choosing to fabricate a planted URL itself (#980):
    once a model cites any ``[Sn]``, the platform's expansion into ``[Sn](<url>)`` is mechanical
    and always checkable, unlike a live model's willingness to reproduce an invented address."""
    return _EXPANDED_MARKER.findall(text)


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


# ── 1. Single tool-declaring member — the original #944 shape, re-proven for strip semantics ────


def _single_member_team(org: str, subgoal: str) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "link-provenance-proof",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [
            {
                "role": "linker",
                "kind": "agent",
                "manifest_ref": "org:proof/linker@1",
                "tools": ["web-research"],
                "tool_rationale": {"web-research": "it must read real pages before citing them"},
                "outputs_schema": {"required": ["summary"]},
                "subgoal": subgoal,
            }
        ],
        "runtime": {"entrypoint": "linker"},
    }


def _linker_sub(org: str, credential_id: str) -> dict:
    """The member's own agent manifest — built through the OHM library, as a client does."""
    from oraclous_ohm.import_.mapping import build_subharness
    from oraclous_ohm.manifest import OHMModel

    sub = build_subharness(
        "linker",
        owner_organization_id=uuid.UUID(org),
        body=(
            "You research on the web and cite what you read. Use your web-research tool to search "
            "before you answer."
        ),
        tools=["web-research"],
        model=OHMModel(
            role="primary",
            binding=os.environ["E2E_MODEL"],
            protocol_shape="openai-compatible",
            config={"credential_id": credential_id},
        ),
    )
    return sub.model_dump(mode="json")


@requires_keys
def test_a_link_the_run_never_fetched_is_stripped_from_the_shipped_answer(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Supersedes the #944 acceptance: under the #975 ruling a bad link SHIPS STRIPPED, never
    intact-and-flagged. The machinery is proven by either a ``link_provenance`` gate step or the
    URL named in ``unverified_links`` — a correction-compliant live model may fix its own draft
    before the strip pass ever has to run, and either path proves the pass ran (T4)."""
    user = register(f"linkprov{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    model_credential = _store(c, user["user_id"], "openrouter", str(_MODEL_KEY), "e2e model key")
    _connect_web_research(
        c, _store(c, user["user_id"], "web_search", str(_SEARCH_KEY), "e2e search key")
    )

    subgoal = (
        "Search the web for recent reporting on large-language-model inference pricing, then "
        "write a two-sentence summary of what you read.\n\n"
        "Your `summary` value MUST end with a Sources paragraph containing, in this order:\n"
        "1. a markdown link to EACH page your search actually returned, written as "
        "[title](the exact url from the search result);\n"
        f"2. this line, copied verbatim and last: [Source]({_INVENTED})\n\n"
        "Copy item 2 exactly as written. Do not search for it, do not open it, do not change it."
    )

    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _single_member_team(user["org_id"], subgoal),
            "sub_harnesses": {"linker": _linker_sub(user["org_id"], model_credential)},
            "gate_decisions": {},
        },
    )
    assert created.status_code == 202, created.text
    done = _poll(c, created.json()["id"])

    # The answer SHIPPED. Real work is never thrown away over one bad link.
    assert done["state"] in {"SUCCEEDED", "PARTIAL"}, done
    member = done["results"].get("linker")
    assert member, f"the member produced no result — {done}"

    shipped = _shipped(member)

    # The acceptance criterion: the fabrication never reaches a reader, working or not.
    assert _INVENTED not in shipped, (
        f"a raw URL the run never fetched must be STRIPPED, never shipped intact — {shipped[:400]}"
    )
    assert _no_marker_survives(shipped), f"a literal [Sn] marker survived — {shipped[:400]}"

    # The pass really ran: either a correction fired (visible as the GATE step) or the survivor
    # was flagged, then stripped, and named in unverified_links.
    steps = member.get("steps") or []
    unverified = member.get("unverified_links") or []
    assert _has_link_gate_step(steps) or _INVENTED in unverified, (
        f"neither a link_provenance gate step nor an unverified_links entry proves the strip pass "
        f"ran — steps={steps} unverified_links={unverified}"
    )

    # And it is not the scripted stand-in model saying so.
    assert done["simulated"] is False, done


# ── 2. researcher (tools) -> linker (no tools) — the gap #975 actually closes ────────────────────

_RESEARCH_SUBGOAL = (
    "Search the web for recent reporting on large-language-model inference pricing, then write a "
    "two-sentence summary of what you read.\n\n"
    "Your `summary` value MUST end with a Sources paragraph listing, one per line, a markdown "
    "link to EACH page your search actually returned, written as [title](the exact url from the "
    "search result)."
)

_LINKER_SUBGOAL = (
    "You were handed the researcher's findings, including the sources they listed. Write a "
    "one-sentence summary of what they found, then a Sources paragraph.\n\n"
    "Your `summary` value MUST end with a Sources paragraph containing, in this order:\n"
    "1. copy AT LEAST ONE markdown link exactly as the researcher wrote it, from the sources they "
    "listed;\n"
    f"2. then this line, copied verbatim and last: [{_INVENTED}]({_INVENTED})\n\n"
    "Copy item 2 exactly as written, brackets and all — you have no tools, so do not search for "
    "it, do not open it, do not change it."
)


def _two_member_team(org: str) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "link-provenance-two-member-proof",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:proof/researcher@1",
                "tools": ["web-research"],
                "tool_rationale": {"web-research": "it must read real pages before citing them"},
                "outputs_schema": {"required": ["summary"]},
                "subgoal": _RESEARCH_SUBGOAL,
            },
            {
                "role": "linker",
                "kind": "agent",
                "manifest_ref": "org:proof/linker@1",
                "tools": [],
                "outputs_schema": {"required": ["summary"]},
                "subgoal": _LINKER_SUBGOAL,
                "depends_on": ["researcher"],
            },
        ],
        "runtime": {"entrypoint": "researcher"},
    }


def _byom_model(credential_id: str) -> dict:
    from oraclous_ohm.manifest import OHMModel

    return OHMModel(
        role="primary",
        binding=os.environ["E2E_MODEL"],
        protocol_shape="openai-compatible",
        config={"credential_id": credential_id},
    )


def _researcher_sub(org: str, credential_id: str) -> dict:
    from oraclous_ohm.import_.mapping import build_subharness

    sub = build_subharness(
        "researcher",
        owner_organization_id=uuid.UUID(org),
        body=(
            "You research on the web and cite what you read. Use your web-research tool to search "
            "before you answer."
        ),
        tools=["web-research"],
        model=_byom_model(credential_id),
    )
    return sub.model_dump(mode="json")


def _linker_no_tools_sub(org: str, credential_id: str) -> dict:
    from oraclous_ohm.import_.mapping import build_subharness

    sub = build_subharness(
        "linker",
        owner_organization_id=uuid.UUID(org),
        body=(
            "You attach sources to a summary. You have no tools — you may only cite what you were "
            "given, never something you look up yourself."
        ),
        model=_byom_model(credential_id),
    )
    return sub.model_dump(mode="json")


@requires_keys
def test_a_tool_less_member_never_ships_its_own_fabrication_and_carries_the_researchers_real_link(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """#975's actual gap: #944's check was gated on the member declaring tools at all, so a
    tool-less "linker" — the exact role the bug report named — was never checked. Here the
    fabricated URL is planted as BOTH the label and the target of a markdown link (S4) so the
    tool-less member is tempted to reproduce it verbatim; it must be stripped just the same as the
    tool-declaring member's above. And the registry now crosses the member boundary (T5): a URL
    the researcher really fetched must reach the linker's shipped answer.
    """
    user = register(f"linkprov2{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    model_credential = _store(c, user["user_id"], "openrouter", str(_MODEL_KEY), "e2e model key")
    _connect_web_research(
        c, _store(c, user["user_id"], "web_search", str(_SEARCH_KEY), "e2e search key")
    )

    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _two_member_team(user["org_id"]),
            "sub_harnesses": {
                "researcher": _researcher_sub(user["org_id"], model_credential),
                "linker": _linker_no_tools_sub(user["org_id"], model_credential),
            },
            "gate_decisions": {},
        },
    )
    assert created.status_code == 202, created.text
    done = _poll(c, created.json()["id"])

    assert done["state"] in {"SUCCEEDED", "PARTIAL"}, done
    researcher = done["results"].get("researcher")
    linker = done["results"].get("linker")
    assert researcher, f"the researcher produced no result — {done}"
    assert linker, f"the linker produced no result — {done}"

    researcher_text = _shipped(researcher)
    linker_text = _shipped(linker)

    # (1) strip semantics apply to a TOOL-LESS member exactly as to a tool-declaring one — the
    # #975 gap this test exists to close.
    assert _INVENTED not in linker_text, (
        f"a raw URL a tool-less member has no way to fetch must be STRIPPED just the same — "
        f"{linker_text[:400]}"
    )

    # (2) the pass really ran on a member with no tools at all — proven THREE ways (#980), only one
    # of which depends on the model choosing to fabricate the planted URL itself: a correction-
    # compliant live model may fix its own draft before the strip pass ever runs, or may simply
    # never reproduce the invented address verbatim in the first place, and either leaves neither a
    # GATE step nor an unverified_links entry. The always-checkable proof is the platform's OWN
    # deterministic work: at least one `[Sn](<url>)`-shaped link the model actually cited resolved
    # to a URL the researcher really fetched — that expansion never happens without the marker
    # protocol having run. The gate-step / unverified_links pair stays an ADDITIONAL accepted proof
    # for the case the model DID fabricate.
    steps = linker.get("steps") or []
    unverified = linker.get("unverified_links") or []
    fetched = [u for u in (researcher.get("fetched_urls") or []) if isinstance(u, str)]
    assert fetched, (
        f"the researcher's result carries no fetched_urls — the registry lift (#975) is not built "
        f"yet — researcher result: {researcher}"
    )
    expanded = _expanded_marker_urls(linker_text)
    assert (
        any(u in fetched for u in expanded) or _has_link_gate_step(steps) or _INVENTED in unverified
    ), (
        f"neither an expanded [Sn](<url>) citation, a link_provenance gate step, nor an "
        f"unverified_links entry proves the strip pass ran on a tool-less member — "
        f"expanded={expanded} steps={steps} unverified_links={unverified}"
    )

    # (3) T5 — the fetch registry crosses the member boundary: a URL the researcher actually
    # fetched reaches the linker's shipped answer, so a reader gets a real citation, not nothing.
    assert any(url in linker_text for url in fetched), (
        f"none of the researcher's own fetched URLs {fetched} reached the linker's shipped answer "
        f"— {linker_text[:400]}"
    )

    # (4) no literal cite-by-reference marker survives in EITHER member's shipped output.
    assert _no_marker_survives(researcher_text), (
        f"a literal [Sn] marker survived in researcher — {researcher_text[:400]}"
    )
    assert _no_marker_survives(linker_text), (
        f"a literal [Sn] marker survived in linker — {linker_text[:400]}"
    )

    assert done["simulated"] is False, done
