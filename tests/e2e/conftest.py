"""Shared fixtures for gateway e2e tests.

Every test in this package drives the DEPLOYED docker stack through the **application-gateway**
(`:8006`) with **real registration → real JWT** — no fakes, no mocks, no internal-function calls, no
DB-direct assertions (FUCK_CLAUDE_FUCK_PAPERCLIP.md / CLAUDE.md §9). The whole package auto-skips
when the gateway is unreachable (the `_require_gateway` autouse fixture), so unit CI stays green.

These are pytest fixtures (auto-discovered) rather than importable helpers on purpose: a test must
never `from tests.e2e.conftest import ...` (that import is not portable under collection — CLAUDE.md
§4.1). Take `register` / `gateway_client` / `gateway_url` as fixture arguments instead.

Fixture note: two draft-validation rules block a team manifest outright, so a new fixture must give
every member tool a `tool_rationale` (`F-TOOL-UNJUSTIFIED`) and declare each member's output keys
(`F-NO-OUTPUT-CONTRACT`). The shapes, the markers per leg, and which key comes from where are in
`tests/e2e/README.md` (#921).
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Generator, Iterator

import httpx
import pytest

GATEWAY = "http://localhost:8006"  # the application-gateway — the ONLY external surface

#: The user's own model key, read from the env (scripts/e2e.sh exports it from deploy/.env.test).
#: Since #724 no service is handed a platform model key, so a stack running a REAL extractor
#: (``KGS_EXTRACTOR=openai``) needs the ORG to have designated one — otherwise every ingest that
#: reaches the extractor fails closed with ``model_credential_not_configured``. The compose DEFAULT
#: is ``KGS_EXTRACTOR=null``, which needs no credential, so CI and a key-free stack are unaffected.
_MODEL_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

#: The ONE model the suite binds (#1000). A test reads it back as ``os.environ["E2E_MODEL"]`` rather
#: than importing this module (see the docstring: conftest is not importable under collection);
#: pytest loads this conftest before any test module in the directory, so by then the env holds
#: either the caller's value or the default set here. Keep the ``openrouter/`` prefix: the harness
#: splits the binding at the FIRST ``/`` for the provider (base URL, policy ``allowed_providers``)
#: and sends the rest to OpenRouter as the model id.
#:
#: As of 2026-09-13 (#1049) the previous default, a FREE OpenRouter model
#: (``nvidia/nemotron-3-super-120b-a12b:free``), had its daily quota exhausted for days straight,
#: failing every real-model e2e test on `main` and every branch with ``LLM call -> 429``. The repo
#: owner ruled: if the free tier is unreliable, default to a cheap but demonstrably strong paid
#: model instead of chasing free-tier availability. The owner picked
#: ``deepseek/deepseek-v4-flash`` ($0.066 in / $0.131 out per million tokens, OpenRouter pricing)
#: with evidence from using it that same day for the #1043 diagnosis and deployed-stack proof: on
#: this repo's deployed stack, through the gateway, it issued a correct real tool call
#: (``manifest-validate``, verdict returned and acted on) and followed a two-part instruction
#: exactly (team JSON, then a separate receipt object) -- precisely the compliance that reproduces
#: #1043 rather than accidentally dodging it. One full compiler replay cost ~4,500 tokens across two
#: model turns, ~16 seconds. That instruction-following point is the one that matters for a *test*
#: model: a model too weak to follow instructions produces FALSE PASSES, not just failures -- it
#: skips the hard shape and the assertion never fires, so "cheap" alone is not the bar.
#:
#: Fallback order if this model proves too weak, in order: ``google/gemini-2.5-flash-lite``
#: ($0.10 / $0.40), then ``openai/gpt-4o-mini`` ($0.15 / $0.60). Do not silently switch off
#: deepseek -- if you move to a fallback, record why here and what specifically failed.
#:
#: Also on 2026-09-13: a ~20-test slice of the full ``byom`` marker was run live on this model as a
#: broader check before trusting it as the default. 15/17 tests that reached a terminal state
#: PASSED; the 2 failures were both a weak-model mechanism (self-judging harshness in a convergence
#: loop, token-budget exhaustion in a tool-heavy research loop), zero were PRODUCT-class or
#: instruction-following breaks. Verdict: cautiously positive, moderate confidence -- worth watching
#: those two mechanisms. Full numbers and the cost measurement: tests/e2e/README.md, #1049.
#:
#: Changing this default does NOT by itself change what CI or the nightly job run: both
#: `.github/workflows/ci.yml` and `.github/workflows/e2e-nightly.yml` set their own ``E2E_MODEL``
#: env var independently as a hardcoded literal fallback --
#: ``${{ vars.E2E_MODEL || 'openrouter/nvidia/nemotron-3-super-120b-a12b:free' }}`` -- not derived
#: from this file. This default only takes effect for a local run with no ``E2E_MODEL`` env var set
#: at all (e.g. `scripts/e2e.sh` with no `deploy/.env.test` value). Making CI/nightly pick up the
#: new default needs either the `vars.E2E_MODEL` GitHub Actions repository variable set, or a
#: workflow-file edit (devops-implementer territory) -- tracked as a follow-up on #1049.
_DEFAULT_E2E_MODEL = "openrouter/deepseek/deepseek-v4-flash"
E2E_MODEL = os.getenv("E2E_MODEL", "").strip() or _DEFAULT_E2E_MODEL
os.environ["E2E_MODEL"] = E2E_MODEL


def _gateway_up() -> bool:
    try:
        return httpx.get(f"{GATEWAY}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(autouse=True)
def _require_gateway() -> None:
    """Skip every e2e test when the deployed gateway is unreachable (keeps unit CI green)."""
    if not _gateway_up():
        pytest.skip("gateway :8006 not reachable")


@pytest.fixture
def gateway_url() -> str:
    return GATEWAY


def _designate_org_model_credential(token: str, user_id: str) -> str | None:
    """Give a freshly registered org its own default model credential (#724), through the real API.

    The user pastes THEIR key via ``POST /credentials/`` and designates it the org default with
    ``PUT /credentials/{id}`` ``default_for="model"`` — the same two calls a real operator makes,
    nothing injected server-side. Returns the credential id, or None when no key is available.

    Needed because #724 removed the platform model key: on a stack running a real extractor an org
    with no designated credential fails every ingest closed. A key-free stack (the compose default
    ``KGS_EXTRACTOR=null``, which is what CI runs) never reaches a model, so this is a no-op there.
    """
    if not _MODEL_KEY:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    created = httpx.post(
        f"{GATEWAY}/credentials/",
        headers=headers,
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": "e2e org model key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _MODEL_KEY},
        },
        timeout=20.0,
    )
    assert created.status_code == 201, f"credential create failed: {created.text}"
    credential_id = created.json()["id"]
    designated = httpx.put(
        f"{GATEWAY}/credentials/{credential_id}",
        headers=headers,
        json={
            "id": credential_id,
            "user_id": user_id,
            "tool_id": str(uuid.uuid4()),
            "provider": "openrouter",
            "cred_type": "api_key",
            "default_for": "model",
        },
        timeout=20.0,
    )
    assert designated.status_code == 200, f"designate default failed: {designated.text}"
    return str(credential_id)


#: Maps an access token back to the refresh token it was issued with (#921), so `gateway_client`
#: can renew a login it did not itself mint. Keyed by access token because every call site passes
#: `gateway_client(user["token"])` — a string, not the whole `register()` dict — and changing that
#: 80+ call-site signature is out of scope here. Populated by `register()`; never cleared (each
#: registration's token is unique for the process lifetime, so this only ever grows within a run).
_REFRESH_TOKENS: dict[str, str] = {}


@pytest.fixture
def register(fail_as: Callable[[str, str], None]) -> Callable[..., dict]:
    """Factory: register a fresh user through the gateway → {token, org_id, user_id, email}.

    ``with_model_credential=True`` also designates the caller's key as the org's default model
    credential (#724), which a test needs only when its ingest reaches a REAL extractor. It is
    opt-in rather than automatic because each registration would otherwise cost two extra gateway
    calls, and across the suite that trips the edge per-IP rate limiter (a 429 on register itself).
    ``model_credential_id`` is None when not requested, or when no key is available.
    """

    def _register(full_name: str = "E2E User", *, with_model_credential: bool = False) -> dict:
        email = f"e2e-{uuid.uuid4().hex[:12]}@studio.test"
        reg = httpx.post(
            f"{GATEWAY}/v1/auth/register",
            json={"email": email, "password": "TestPass123", "full_name": full_name},
            timeout=15.0,
        )
        assert reg.status_code == 201, f"register failed: {reg.status_code} {reg.text}"
        reg_body = reg.json()
        token = reg_body["access_token"]
        _REFRESH_TOKENS[token] = reg_body["refresh_token"]
        me_response = httpx.get(
            f"{GATEWAY}/v1/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=15.0
        )
        # #850 / #1061: a throttled read-back (the sign-up rate limiter) used to surface as
        # `KeyError: 'organisation_id'` several lines later, in a test that had nothing to do with
        # auth. Name the status here instead — the test's own scaffolding hit a limiter, not a
        # product bug (#921 TEST-SETUP).
        if me_response.status_code != 200:
            fail_as(
                "TEST-SETUP",
                f"/v1/auth/me failed: {me_response.status_code} {me_response.text}",
            )
        me = me_response.json()
        return {
            "token": token,
            "refresh_token": reg_body["refresh_token"],
            "org_id": me["organisation_id"],
            "user_id": me["id"],
            "email": email,
            "model_credential_id": (
                _designate_org_model_credential(token, me["id"]) if with_model_credential else None
            ),
        }

    return _register


class _RenewingAuth(httpx.Auth):
    """Bearer auth that renews the access token through the gateway's public refresh endpoint on a
    401 (#921), the same way a real user's client would — never a server-minted token.

    Some e2e tests (the real-model loop tests) poll for 20+ minutes, which outlives the 30-min
    access token (`USER_ACCESS_TOKEN_TTL_MINUTES`). On a 401 this calls ``POST /v1/auth/refresh``
    with the refresh token issued at registration, swaps in the fresh access/refresh pair (the
    refresh token rotates — auth-service revokes the whole family on reuse), and retries the ONE
    failed request once. If the refresh itself fails, the original 401 is returned unchanged —
    never masked.
    """

    def __init__(self, gateway: str, access_token: str, refresh_token: str) -> None:
        self._gateway = gateway
        self._access_token = access_token
        self._refresh_token = refresh_token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._access_token}"
        response = yield request
        if response.status_code != 401:
            return
        response.read()  # drain the failed response before issuing a new request on the same conn
        refreshed = httpx.post(
            f"{self._gateway}/v1/auth/refresh",
            json={"refresh_token": self._refresh_token},
            timeout=15.0,
        )
        if refreshed.status_code != 200:
            return  # refresh itself failed — surface the original 401, never mask it
        body = refreshed.json()
        self._access_token = body["access_token"]
        self._refresh_token = body["refresh_token"]
        request.headers["Authorization"] = f"Bearer {self._access_token}"
        yield request


@pytest.fixture
def gateway_client() -> Iterator[Callable[[str], httpx.Client]]:
    """Factory for httpx clients bound to the gateway + a JWT; all are closed at teardown.

    When ``token`` came from ``register()`` the client renews its own login on a 401 via
    ``_RenewingAuth`` instead of failing outright (#921). A token this fixture has no refresh
    token for (e.g. a deliberately invalid one in an auth-failure test) keeps today's behaviour: a
    plain, non-renewing bearer header.
    """
    opened: list[httpx.Client] = []

    def _client(token: str) -> httpx.Client:
        refresh_token = _REFRESH_TOKENS.get(token)
        if refresh_token is not None:
            c = httpx.Client(
                base_url=GATEWAY, auth=_RenewingAuth(GATEWAY, token, refresh_token), timeout=30.0
            )
        else:
            c = httpx.Client(
                base_url=GATEWAY, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
            )
        opened.append(c)
        return c

    yield _client
    for c in opened:
        c.close()


# ── Failure-class taxonomy (#921) ─────────────────────────────────────────────────────────────────
#
# A real-model e2e failure that is not a product bug must still fail the run -- it is never
# skipped, xfailed, or swallowed -- but it must be LABELLED, so a reviewer does not have to
# re-derive "not our bug" by hand every time. Four classes, PRODUCT is the default:
#   PRODUCT       -- any unlabelled failure (the default: an author who forgets to classify a new
#                    failure gets the conservative "assume it's our bug" reading).
#   PROVIDER      -- the model provider itself refused the call (#1049: rate limit / auth / timeout
#                    / 5xx), before product logic ever ran.
#   MODEL-QUALITY -- the product worked, but the real model's answer fell short of the test's bar
#                    (an evaluator score below threshold, a loop that never converged).
#   TEST-SETUP    -- the test's own scaffolding broke, not the product under test (a poll got a
#                    non-2xx, registration hit the sign-up limiter).
# See tests/e2e/README.md for the full rules on when a test author may use each.
_FAILURE_TAG_RE = re.compile(r"\[e2e-failure:([A-Za-z-]+)\]")
_KNOWN_FAILURE_CLASSES = ("PROVIDER", "MODEL-QUALITY", "TEST-SETUP")
_PRODUCT_CLASS = "PRODUCT"


@pytest.fixture
def fail_as() -> Callable[[str, str], None]:
    """Factory: ``fail_as(kind, message)`` raises an AssertionError tagged for the failure-class
    taxonomy above (#921). It still fails the test -- the tag only tells the terminal summary and
    junit XML *why*, so a provider refusal or a real model's weak answer is not read as a product
    bug. ``kind`` is one of PROVIDER / MODEL-QUALITY / TEST-SETUP -- never pass PRODUCT, it is the
    default for anything left untagged.
    """

    def _fail(kind: str, message: str) -> None:
        if kind not in _KNOWN_FAILURE_CLASSES:
            raise ValueError(
                f"unknown e2e failure class {kind!r} -- want one of {_KNOWN_FAILURE_CLASSES}"
            )
        pytest.fail(f"[e2e-failure:{kind}] {message}", pytrace=False)

    return _fail


# ── Legible environment failures (#1049) ──────────────────────────────────────────────────────────
#
# A provider refusal (rate limit / auth / timeout / 5xx) that happens before the model ever gets to
# exercise product logic must not present as a bare product assertion failure. Before this, a FAILED
# run showed as `assert 'FAILED' == 'SUCCEEDED'` and a reviewer had to open the run body, find
# `error_message`, and manually recognise `LLM call → 429` as "not our bug" — every single time. The
# harness's own `LLMClientError` (domain/llm/openai_compatible.py) already names this precisely:
# `f"LLM call → {sc}"` for a non-2xx, or `"LLM call transport error: ..."` for a network failure.
# That message reaches a run's `error_message` verbatim or wrapped in a curated sentence
# (execution-engine-service's failure-summary seam), never the provider's raw body (leak-safety), so
# matching the coarse shape below is reliable without needing the body.
_PROVIDER_REFUSAL_RE = re.compile(r"LLM call → (\d{3})|LLM call transport error")


def _provider_refusal_reason(error_message: str | None) -> str | None:
    """None when ``error_message`` looks like a product failure; else a short, human reason.

    A non-2xx/transport failure that reached the harness's own ``LLMClientError`` (rate limit,
    auth, timeout, 5xx) is an ENVIRONMENT condition (#1049), not a defect in the code under test —
    the call never got far enough to exercise product logic.
    """
    if not error_message:
        return None
    match = _PROVIDER_REFUSAL_RE.search(error_message)
    if not match:
        return None
    code = match.group(1)
    if code == "429":
        return f"the provider rate-limited the request (429) — {error_message}"
    if code and code.startswith("5"):
        return f"a provider/upstream error ({code}) — {error_message}"
    if code:
        return f"the provider rejected the request ({code}) — {error_message}"
    return f"a transport failure reaching the provider — {error_message}"


# ── Loop poll budgets scale with max_rounds, not a fixed try count (#921) ─────────────────────────
#
# Nightly service logs showed each loop member turn taking 2-3 min on the real model, with the
# first evaluation landing ~7 min in — a fixed try-count poll (e.g. 150 tries * 3s = 450s) declares
# the run failed while it is still progressing. Scale the deadline with the loop's own declared
# max_rounds instead. Per-round ceiling defaults to 180s, overridable via
# E2E_LOOP_ROUND_CEILING_S for a slower model. The nightly job budget is 300 min
# (.github/workflows/e2e-nightly.yml) — keep this default well inside it across the whole
# loop-marked slice.
_LOOP_ROUND_CEILING_S = float(os.getenv("E2E_LOOP_ROUND_CEILING_S", "180"))
_LOOP_POLL_STARTUP_ALLOWANCE_S = 60.0


def loop_poll_budget_s(max_rounds: int) -> float:
    """Deadline (seconds) for polling a real-model loop run to a terminal state (#921).

    ``max_rounds * per-round ceiling + a fixed startup allowance`` — see the module comment above
    for why a fixed try count undercounts a real model.
    """
    return max_rounds * _LOOP_ROUND_CEILING_S + _LOOP_POLL_STARTUP_ALLOWANCE_S


@pytest.fixture
def loop_poll_budget() -> Callable[[int], float]:
    """Fixture form of ``loop_poll_budget_s`` — take this instead of importing the module (the
    package docstring above: a test must never ``from tests.e2e.conftest import ...``)."""
    return loop_poll_budget_s


@pytest.fixture
def assert_run_succeeded(fail_as: Callable[[str, str], None]) -> Callable[..., None]:
    """Assert a run/agent-execute response reached SUCCEEDED — legibly telling an upstream provider
    refusal (PROVIDER, #1049) apart from a genuine product failure.

    Pass the parsed JSON body and the key holding its terminal state: ``state_key="status"`` for
    ``POST /v1/harnesses/execute``'s response, ``state_key="state"`` for a polled team-run. A
    provider refusal (429 / 5xx / transport, matched against the harness's own ``LLMClientError``
    message shape) fails loudly with a ``[e2e-failure:PROVIDER]``-tagged message naming the reason,
    instead of the bare ``assert 'FAILED' == 'SUCCEEDED'`` a reviewer used to have to re-derive by
    hand every time (#1049). Anything else still fails as a normal assertion — this never turns a
    real failure green, it only makes an environmental one look different from a product one.
    """

    def _assert(body: dict, *, state_key: str = "status", succeeded: str = "SUCCEEDED") -> None:
        state = body.get(state_key)
        if state != succeeded:
            reason = _provider_refusal_reason(body.get("error_message"))
            if reason is not None:
                fail_as(
                    "PROVIDER",
                    f"not a product failure — {reason}. The model provider refused the call "
                    "before product logic ran; this is the test model's quota or availability, "
                    f"not a regression (see tests/e2e/README.md, issue #1049). Full body: {body}",
                )
        assert state == succeeded, body

    return _assert


# ── Group failed e2e tests by class in the terminal summary (#921) ───────────────────────────────
#
# Every class above still counts as a FAILURE in the exit code and the counts pytest prints — this
# hook only adds a grouped breakdown afterwards so a reviewer can tell, at a glance, whether a red
# run needs a product fix or is a labelled provider/model-quality/setup condition. The class is also
# recorded as a `user_properties` entry so junit XML (CI's artifact) carries it too.


def _failure_class_of(report: pytest.TestReport) -> str:
    """PRODUCT unless the failure's own message carries a known ``[e2e-failure:*]`` tag."""
    longrepr = str(getattr(report, "longrepr", "") or "")
    match = _FAILURE_TAG_RE.search(longrepr)
    if match and match.group(1) in _KNOWN_FAILURE_CLASSES:
        return match.group(1)
    return _PRODUCT_CLASS


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo
) -> Generator[None, None, None]:
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        report.user_properties.append(("e2e_failure_class", _failure_class_of(report)))


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    """Print failed e2e tests grouped by failure class (#921), PRODUCT first."""
    failed = terminalreporter.stats.get("failed", [])
    if not failed:
        return
    by_class: dict[str, list[str]] = {}
    for report in failed:
        cls = dict(report.user_properties).get("e2e_failure_class", _PRODUCT_CLASS)
        by_class.setdefault(cls, []).append(report.nodeid)
    terminalreporter.write_sep("=", "e2e failures by class (#921)")
    ordered_classes = [_PRODUCT_CLASS, *sorted(c for c in by_class if c != _PRODUCT_CLASS)]
    for cls in ordered_classes:
        nodeids = by_class.get(cls)
        if not nodeids:
            continue
        terminalreporter.write_line(f"{cls}: {len(nodeids)}")
        for nodeid in nodeids:
            terminalreporter.write_line(f"  {nodeid}")
