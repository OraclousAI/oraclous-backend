"""#961 rulings 1 and 2, engine half — the person's list of sites travels as a THING, not as prose.

Ruled 2026-09-08: a person's list of websites BINDS the run. Leaving it advisory was offered and
refused. The list is checked immediately before each search, in the harness runtime, which means
the harness has to receive it as a list of addresses — not as one line of text inside a larger
request that a member may read, act on, or drop.

Today it is exactly that line of text. ``fold`` joins the filled-in form into labelled lines and
``to_run_inputs`` puts the block under the team's single declared input key, so ``Source addresses:
theverge.com`` arrives as prose. #951's original report is a run that read that prose and ignored
it while satisfying every gate the run has.

So the restriction needs its own channel from the form to the harness, and this file pins the
engine's three links of it:

1. **It survives the create gate.** ``validate_input_keys`` fail-closes any ``inputs`` key the team
   does not declare (#714 defect (b)), so an unknown key is a 422 and the app cannot start. The
   restriction becomes an engine-reserved key beside ``_refresh_seed`` (#602) and ``answers``
   (#846) — read by the engine on every team's behalf, never by the team itself.
2. **A bad address is refused at create.** Before the run is persisted or enqueued, so a mistyped
   address costs one message rather than a run somebody waited for. #951's live finding is why it
   cannot wait: the search vendor accepts a full URL with an ordinary 200 and silently drops the
   restriction, so a bad value does not fail loudly downstream — it produces a normal-looking run
   that searched the whole web.
3. **It reaches every member's dispatch.** A key that clears the gate but that nothing reads is the
   SILENT DISCARD #714 closed. The engine hands it to the harness on each member's execute call.

**The scope guard is half of this file.** Most runs name no sites and must be byte-for-byte
unchanged: no new key, no new kwarg, nothing sent. That is asserted, not assumed.

RED-by-design until the ``[impl]`` lands: ``SITE_RESTRICTION_KEY``, ``validate_site_restriction``
and ``resolve_run_sites`` do not exist yet, so every seam is imported function-locally (§4.1).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.manifest import OHMMember
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_SITES = ["theverge.com", "bbc.co.uk"]


def _team_document(*, task_input: dict[str, Any] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "news-roundup",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:x/researcher@1",
                "subgoal": "gather this week's tech news",
            }
        ],
        "runtime": {"entrypoint": "researcher"},
    }
    if task_input is not None:
        doc["task_input"] = task_input
    return doc


def _key() -> str:
    from oraclous_execution_engine_service.domain.app_form import (  # §4.1 seam
        SITE_RESTRICTION_KEY,
    )

    return SITE_RESTRICTION_KEY


class _RecordingHarness:
    """Records the keyword arguments of every member dispatch and always succeeds."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, *, input_text: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ran", "steps": []}


def _member(role: str = "researcher") -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", subgoal="work")


async def _dispatch_once(inputs: dict[str, Any] | None) -> dict[str, Any]:
    """Run one member through the real dispatch factory and return the kwargs the harness saw."""
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        make_harness_dispatch,
        resolve_run_sites,
    )

    harness = _RecordingHarness()
    dispatch = make_harness_dispatch(
        harness,  # type: ignore[arg-type]
        {},
        required_sites=resolve_run_sites(inputs),
    )
    await dispatch(_member(), [], None)
    return harness.calls[0]


# ── link 1: the key survives the create gate, and nothing else about it moves ────────────────────


def test_the_site_restriction_key_passes_the_undeclared_key_gate() -> None:
    """Without this the app cannot start at all: the key is a 422 and the run never exists."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_input_keys,
    )

    validate_input_keys(load_ohm(_team_document()), {_key(): _SITES})


def test_another_undeclared_key_is_still_a_422() -> None:
    """The gate is unchanged for every key that is not this one — riding alongside launders
    nothing. This is the #846 precedent's own guard, and it earns its place for the same reason."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_input_keys,
    )

    with pytest.raises(TeamRunError) as err:
        validate_input_keys(load_ohm(_team_document()), {_key(): _SITES, "pr_url": "https://x/1"})
    assert err.value.status_code == 422
    assert err.value.error_type == "undeclared_input_key"
    assert "pr_url" in str(err.value)


def test_the_422_message_does_not_advertise_the_engine_reserved_key() -> None:
    """The "it consumes …" list exists to tell a caller which of THEIR team's keys to use. Naming
    an engine-internal one there teaches people to reach for it by hand."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_input_keys,
    )

    with pytest.raises(TeamRunError) as err:
        validate_input_keys(
            load_ohm(_team_document(task_input={"key": "task", "required": True})),
            {"pr_url": "https://x/1"},
        )
    assert _key() not in str(err.value)


# ── link 2: a bad address is refused before anything is spent ────────────────────────────────────


def test_a_publication_name_in_the_restriction_is_a_422_at_create() -> None:
    """ "BBC News" is not an address and there is no mechanical route from one to the other (#951).
    Refused here, where the person is still looking at the screen."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_site_restriction,
    )

    with pytest.raises(TeamRunError) as err:
        validate_site_restriction({_key(): ["BBC News"]})
    assert err.value.status_code == 422


def test_the_refusal_names_the_address_it_could_not_read() -> None:
    """A refusal that does not say WHICH entry was wrong leaves the person guessing across their
    whole list — the #692/#693 shape, where a member told "409" could only retry blindly."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_site_restriction,
    )

    with pytest.raises(TeamRunError) as err:
        validate_site_restriction({_key(): ["theverge.com", "BBC News"]})
    assert "BBC News" in str(err.value)


def test_a_run_with_no_restriction_passes_the_create_gate_untouched() -> None:
    """The scope guard at create: an absent or empty list is not a restriction and not a fault.

    The key is resolved INSIDE the test, never in the parametrize list: a seam read at collection
    time aborts the whole run rather than failing this one test (§4.1).
    """
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_site_restriction,
    )

    for payload in (None, {}, {"task": "hello"}, {_key(): []}):
        validate_site_restriction(payload)


# ── link 3: it reaches the member's dispatch ─────────────────────────────────────────────────────


async def test_the_restriction_reaches_the_members_harness_call() -> None:
    """The half that makes the binding real. A key the engine accepts and then never forwards is
    #714's silent discard: the person's restriction vanishes and the member searches the whole web
    with nothing to say it was ever asked not to."""
    kwargs = await _dispatch_once({_key(): _SITES})

    assert kwargs["required_sites"] == _SITES


async def test_a_pasted_link_reaches_the_harness_as_a_bare_address() -> None:
    """The engine cleans ONCE, here, and the harness compares against the cleaned list.

    Sending the raw text would put the cleaning rule in two places, which is #946's "two cleaning
    passes that disagreed" defect. The shared kernel cleaner is a fixed point, so the harness may
    clean the model's own argument the same way and the two halves still meet.
    """
    kwargs = await _dispatch_once({_key(): ["https://www.theverge.com/tech"]})

    assert kwargs["required_sites"] == ["theverge.com"]


async def test_a_run_that_named_no_sites_sends_no_restriction_at_all() -> None:
    """The scope guard at the dispatch. Not an empty list travelling as a kwarg — nothing.

    An empty list arriving downstream is one refactor away from reading as "restrict to no sites",
    which would refuse every search on every ordinary run in the platform.
    """
    kwargs = await _dispatch_once({"task": "round up this week's tech news"})

    assert "required_sites" not in kwargs
