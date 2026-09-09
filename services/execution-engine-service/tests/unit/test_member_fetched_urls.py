"""#975 — threading the fetch registry member-to-member through the engine (plan §5, slice T5).

Issue #944 shipped a per-member post-hoc check (``unverified_links``, pinned in
``test_member_unverified_links.py``). #975 found the gap it left open: a tool-less member (e.g. a
"linker" role) can still write a URL from training data. The fix, cite-by-reference, needs the
harness-runtime loop to know what a run has ALREADY fetched before this member's own turn — the
engine is the only party that can tell it, because the loop only ever sees one member's own tool
calls.

This file pins the ENGINE's half of that: ``run_team_harness``/``make_harness_dispatch`` (in
``services/team_run.py``) hold a per-role contribution map and thread it into each dispatch as two
new keyword-only args on the ``_Harness.execute()`` call:

* ``prior_fetched_urls`` — the union of this member's DIRECT upstream roles' own contributions,
  composed in MANIFEST DECLARATION ORDER (``depends_on``), never completion order (A5/T11).
* ``person_supplied_text`` — the run's task text + rendered intake answers, sent to EVERY member
  including the entrypoint, and containing nothing envelope- or member-authored (A10/S7).

A member's own CONTRIBUTION is the delta: ``result.get("fetched_urls")`` minus what it was sent,
never its full return — so a downstream member is never re-seeded transitively with an ancestor's
URL it did not fetch itself (A10). The full registry a member reports is still lifted verbatim into
its stored ``results[role]["fetched_urls"]``, next to ``unverified_links`` (A4) — and on a
gate-resume drive that map is rebuilt from ``completed`` rather than re-dispatching.

RED until the ``[impl]`` lands: ``run_team_harness``/``make_harness_dispatch`` do not build the
contribution map, never send either kwarg (the stub below accepts arbitrary kwargs, so this fails on
BEHAVIOUR — a missing/None value — never a TypeError), never lift ``fetched_urls`` into
``results``, and ``HarnessClient.execute()`` has no such parameters at all (its own two tests below
are the one place in this file that fails on a TypeError, mirroring the T2/T4 precedent for a
brand-new kwarg on an existing call).

Ambiguities this slice resolved (none of these are pinned elsewhere in the plan):

* **``person_supplied_text`` composition.** Task text, then confirmed answers rendered via the
  already-existing ``_render_answers``, then hypotheses rendered the same way — each present part
  joined by a blank line, an absent part (no task / no confirmed / no hypotheses) simply omitted
  rather than rendered empty. No directive headers (``CONFIRMED_ANSWERS_HEADER`` etc.) — those exist
  to tell a MODEL how to treat the two blocks inside its normal input, not to shape the raw
  citable-URL-mining text.
* **Bounds "when collected" (S3).** Applied at both the per-member ``results`` lift (mirroring the
  ``unverified_links`` ``isinstance`` filter exactly) and the delta a member contributes downstream:
  a non-string entry is dropped, a string over 2048 chars is dropped (not truncated), and the
  registry a single member reports is capped at 2000 entries, head stable (drop the tail) — the same
  convention the repository layer's ordered union already uses (T14).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from oraclous_execution_engine_service.services.harness_client import HarnessClient
from oraclous_execution_engine_service.services.team_run import _render_answers, run_team_harness
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime, OHMTaskInput

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")

_A1 = "https://source.test/a1"
_A2 = "https://source.test/a2"
_B1 = "https://source.test/b1"
_C1 = "https://source.test/c1"


def _m(role: str, **over: Any) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", **over)


def _team(members: list[OHMMember], **over: Any) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
        **over,
    )


def _diamond_team() -> OHMManifest:
    # a -> (b, c) -> d ; d.depends_on declares b BEFORE c.
    return _team(
        [
            _m("a"),
            _m("b", depends_on=["a"]),
            _m("c", depends_on=["a"]),
            _m("d", depends_on=["b", "c"]),
        ]
    )


class _RecordingHarness:
    """Captures every kwarg ``execute()`` receives, keyed by the dispatched role, and supports a
    per-role async delay — the template every scenario in this file drives (mirrors #944's
    ``_ScriptedHarness`` in ``test_member_unverified_links.py``)."""

    def __init__(
        self,
        *,
        fetched_urls: dict[str, list[Any]] | None = None,
        outputs: dict[str, str] | None = None,
        delays: dict[str, float] | None = None,
    ) -> None:
        self._fetched_urls = fetched_urls or {}
        self._outputs = outputs or {}
        self._delays = delays or {}
        self.calls: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []  # completion order, distinct from dispatch/declaration order

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        ref = str(kwargs.get("manifest_ref") or "")
        role = ref.split("/")[-1].split("@")[0]
        self.calls[role] = kwargs
        delay = self._delays.get(role)
        if delay:
            await asyncio.sleep(delay)
        self.order.append(role)
        result: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": self._outputs.get(role, f"{role}-out"),
        }
        if role in self._fetched_urls:
            result["fetched_urls"] = self._fetched_urls[role]
        return result


# ── 1. Diamond isolation + manifest declaration order (T11) ────────────────────────────────────


async def test_diamond_isolation_and_manifest_declaration_order() -> None:
    harness = _RecordingHarness(
        fetched_urls={"a": [_A1, _A2], "b": [_B1], "c": [_C1]},
        delays={"b": 0.05, "c": 0.0},  # b and c both start after a; b is made to finish LAST
    )
    await run_team_harness(_diamond_team(), harness)

    # the delay really did flip completion order — otherwise the assertion on `d` below would pass
    # for the wrong reason (a completion-order coincidence, not manifest declaration order)
    assert harness.order.index("c") < harness.order.index("b")

    assert harness.calls["a"].get("prior_fetched_urls") == []  # the entrypoint: nothing upstream
    assert harness.calls["b"].get("prior_fetched_urls") == [_A1, _A2]  # a's contribution only
    assert harness.calls["c"].get("prior_fetched_urls") == [_A1, _A2]  # a's contribution only, too
    # d: manifest declaration order (depends_on=["b", "c"]) — NEVER completion order (b finished
    # last above, yet b's contribution still leads).
    assert harness.calls["d"].get("prior_fetched_urls") == [_B1, _C1]


async def test_a_shared_url_is_deduped_only_when_composing_the_downstream_seed() -> None:
    # A5b: a URL two direct upstreams BOTH fetched appears in each of their own contribution lists
    # (not deduped at the source) but only once in a downstream member's composed seed.
    shared = "https://source.test/shared"
    harness = _RecordingHarness(
        fetched_urls={"a": [_A1], "b": [_B1, shared], "c": [_C1, shared]},
    )
    res = await run_team_harness(_diamond_team(), harness)

    assert res.results["b"]["fetched_urls"] == [_B1, shared]  # b's own report keeps the shared URL
    assert res.results["c"]["fetched_urls"] == [_C1, shared]  # so does c's — no cross-dedup here
    # d's composed seed dedupes only when UNIONING the two contributions (b's entries, then c's)
    assert harness.calls["d"].get("prior_fetched_urls") == [_B1, shared, _C1]


# ── 2. Delta collect, never re-seeded transitively (A10) ────────────────────────────────────────


async def test_a_members_contribution_is_the_delta_it_added_not_its_full_return() -> None:
    # p -> mid -> down. mid is SENT p's contribution and echoes it back plus one genuinely new entry
    # — only that new entry is mid's OWN contribution, so `down` (mid's only direct upstream) is
    # never re-seeded with p's URL transitively; it only ever sees what mid itself added.
    team = _team([_m("p"), _m("mid", depends_on=["p"]), _m("down", depends_on=["mid"])])
    harness = _RecordingHarness(fetched_urls={"p": [_A1], "mid": [_A1, _B1]})
    res = await run_team_harness(team, harness)

    assert harness.calls["mid"].get("prior_fetched_urls") == [_A1]  # seeded from p
    assert res.results["mid"]["fetched_urls"] == [_A1, _B1]  # the FULL registry lift, unchanged
    # down's seed is mid's DELTA only ([_B1]) — never _A1, which down must never see transitively
    assert harness.calls["down"].get("prior_fetched_urls") == [_B1]


# ── 3. person_supplied_text: task + answers, to every member, nothing member-authored (A10/S7) ─


_TASK_TEXT = "Investigate the vendor's public pricing page."
_SENTINEL = "SENTINEL-98765-must-not-leak-into-person-supplied-text"


def _task_team(members: list[OHMMember]) -> OHMManifest:
    return _team(members, task_input=OHMTaskInput())


async def test_person_supplied_text_reaches_every_member_including_the_entrypoint() -> None:
    confirmed = [{"question": "What's the product?", "answer": "Widgets", "hypothesis": False}]
    hypotheses = [{"question": "Is the market growing?", "answer": None, "hypothesis": True}]
    manifest = _task_team([_m("p"), _m("q", depends_on=["p"])])
    inputs = {"task": _TASK_TEXT, "answers": confirmed + hypotheses}
    harness = _RecordingHarness(outputs={"p": f"work done. {_SENTINEL}"})
    await run_team_harness(manifest, harness, inputs=inputs)

    # Pinned by this slice (not fixed elsewhere): task text, then confirmed answers rendered via
    # `_render_answers`, then hypotheses rendered the same way, blank-line separated; an absent part
    # is omitted rather than rendered empty.
    expected = "\n\n".join([_TASK_TEXT, _render_answers(confirmed), _render_answers(hypotheses)])
    assert harness.calls["p"].get("person_supplied_text") == expected  # the entrypoint too
    assert harness.calls["q"].get("person_supplied_text") == expected  # and every downstream member

    # never envelope payload or another member's output, even though it DOES reach the normal input
    assert _SENTINEL in harness.calls["q"]["input_text"]  # sanity: p's output really is handed on
    assert _SENTINEL not in (harness.calls["q"].get("person_supplied_text") or "")


async def test_person_supplied_text_with_no_task_or_answers_is_empty_not_omitted() -> None:
    # A run with neither a task nor answers still gets the kwarg (never omitted, ruling 6/S7) — an
    # empty string, since every part is absent.
    harness = _RecordingHarness()
    await run_team_harness(_team([_m("solo")]), harness)
    assert harness.calls["solo"].get("person_supplied_text") == ""


# ── 4. results lift survives a gate-resume rebuild without re-dispatching (A4) ─────────────────


async def test_gate_resume_seeds_downstream_from_completed_without_redispatching_upstream() -> None:
    team = _team([_m("a"), _m("b", depends_on=["a"])])
    completed = {
        "a": {
            "output": "a-out",
            "status": "SUCCEEDED",
            "steps": [],
            "driving_signals": [],
            "simulated": False,
            "unverified_links": [],
            "fetched_urls": [_A1, _A2],
        }
    }
    harness = _RecordingHarness(fetched_urls={"b": [_B1]})
    res = await run_team_harness(team, harness, completed=completed)

    assert "a" not in harness.calls  # already delivered in a prior drive — never re-dispatched
    assert harness.calls["b"].get("prior_fetched_urls") == [_A1, _A2]  # rebuilt from `completed`
    assert res.results["a"]["fetched_urls"] == [_A1, _A2]  # reused verbatim, not re-derived


# ── 5. Bounds on what the engine collects (S3) ──────────────────────────────────────────────────


async def test_a_non_string_fetched_url_entry_is_filtered_out() -> None:
    harness = _RecordingHarness(fetched_urls={"a": ["https://x.test/ok", 123, None, {"x": 1}]})
    res = await run_team_harness(_team([_m("a"), _m("b", depends_on=["a"])]), harness)
    assert res.results["a"]["fetched_urls"] == ["https://x.test/ok"]
    assert harness.calls["b"].get("prior_fetched_urls") == ["https://x.test/ok"]


async def test_an_entry_over_2048_chars_is_dropped_not_truncated() -> None:
    ok_url = "https://x.test/ok"
    long_url = "https://x.test/" + "a" * 2040
    assert len(long_url) > 2048
    harness = _RecordingHarness(fetched_urls={"a": [ok_url, long_url]})
    res = await run_team_harness(_team([_m("a"), _m("b", depends_on=["a"])]), harness)
    assert res.results["a"]["fetched_urls"] == [ok_url]
    assert long_url not in (harness.calls["b"].get("prior_fetched_urls") or [])


async def test_a_members_registry_is_capped_at_2000_entries_head_stable() -> None:
    many = [f"https://x.test/{i}" for i in range(2005)]
    harness = _RecordingHarness(fetched_urls={"a": many})
    res = await run_team_harness(_team([_m("a"), _m("b", depends_on=["a"])]), harness)
    assert len(res.results["a"]["fetched_urls"]) == 2000
    assert res.results["a"]["fetched_urls"] == many[:2000]  # head stable — the tail is dropped
    assert len(harness.calls["b"].get("prior_fetched_urls") or []) <= 2000


# ── 6. Real client marshals the two new kwargs (mirrors test_harness_client.py) ─────────────────


def _mock_client(handler: Any) -> HarnessClient:
    return HarnessClient(
        "http://harness", headers={"X-Internal-Key": "k"}, transport=httpx.MockTransport(handler)
    )


async def test_execute_never_omits_prior_fetched_urls_or_person_supplied_text() -> None:
    # Ruling 6/S7: a version-skewed harness must not be able to launder a missing value into "not
    # sent" — so even a caller that supplies NEITHER kwarg must still see them in the body, as the
    # empty defaults ([] / ""). No new kwarg is passed here, so this fails on BEHAVIOUR alone, not
    # a TypeError: today the keys are simply absent.
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x", "status": "SUCCEEDED", "output": "done"})

    await _mock_client(handler).execute(input_text="go", manifest_inline={"ohm_version": "1.0"})
    assert captured["body"].get("prior_fetched_urls") == []
    assert captured["body"].get("person_supplied_text") == ""


async def test_execute_marshals_prior_fetched_urls_and_person_supplied_text_when_given() -> None:
    # The forward-facing half of the same contract: real values reach the body under exactly these
    # key names. `execute()` has no such parameters today, so this fails with a TypeError — the same
    # brand-new-kwarg RED shape pinned by slices T2/T4 for the loop/service side of this feature.
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x", "status": "SUCCEEDED", "output": "done"})

    await _mock_client(handler).execute(
        input_text="go",
        manifest_inline={"ohm_version": "1.0"},
        prior_fetched_urls=[_A1],
        person_supplied_text="hello",
    )
    assert captured["body"]["prior_fetched_urls"] == [_A1]
    assert captured["body"]["person_supplied_text"] == "hello"
