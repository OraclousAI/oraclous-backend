#!/usr/bin/env python3
"""BEFORE/AFTER concurrency probe for oraclous-backend issue #1029.

Measures whether the single-uvicorn-worker auth-service stalls unrelated requests
(GET /v1/auth/me) while it is busy running synchronous bcrypt work for concurrent
POST /v1/auth/login calls.

Everything goes through the application gateway (default http://localhost:8006) —
never a service port directly, never an /internal path (repo RULE 5).

This is a hand-run measurement tool for issue #1029, not a pytest test and not part
of CI. It asserts nothing — it only reports wall-clock latency distributions, which
would flake under CI's shared/throttled hardware. Run it manually against a real,
freshly-started deployed stack before and after the #1029 change to compare numbers.

Usage:
    python scripts/auth_concurrency_probe.py --out /path/to/result.json
    python scripts/auth_concurrency_probe.py --gateway http://localhost:8006 --out /tmp/probe.json

Phases:
    A. Setup   — register N fresh users sequentially (spaced out, nothing concurrent).
    B. Quiet   — poll GET /v1/auth/me sequentially with nothing else in flight.
    C. Load    — start a background poller hitting GET /v1/auth/me every 20ms, then fire
                 all N POST /v1/auth/login requests concurrently via asyncio.gather.
                 Stop the poller once logins finish.

Prints a summary (min/median/p95/max for quiet vs under-load /me, and the login
latencies) and writes the same data as JSON to the file given by the required --out
argument (parent directories are created if they don't exist).

Tolerant of failures: any non-2xx from register/login/me is printed (status + body)
and the script exits non-zero rather than producing numbers derived from a broken
setup. A 429 is called out explicitly since the edge rate limiter is a known confound.

Every reported number is computed from the recorded latencies here — nothing is
hand-derived or hardcoded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

DEFAULT_GATEWAY = "http://localhost:8006"
NUM_USERS = 8
QUIET_POLL_COUNT = 40
LOAD_POLL_INTERVAL_S = 0.020  # 20ms
REGISTER_SPACING_S = 0.25  # keep phase A strictly non-concurrent
REQUEST_TIMEOUT_S = 30.0
PROBE_PASSWORD = "ProbePass123"  # noqa: S105 — fixed password for throwaway probe accounts, not a secret


class ProbeFailure(Exception):
    """Raised when the probe itself hits a broken precondition (non-2xx, 429, ...).

    Kept as a normal exception (rather than sys.exit deep inside async code, including
    a background asyncio task) so cleanup and the final error report happen in one
    place in main().
    """


@dataclass
class User:
    email: str
    password: str
    token: str


@dataclass
class ProbeResult:
    quiet_latencies_ms: list[float] = field(default_factory=list)
    load_latencies_ms: list[float] = field(default_factory=list)
    login_latencies_ms: list[float] = field(default_factory=list)
    register_latencies_ms: list[float] = field(default_factory=list)


def check_response(resp: httpx.Response, what: str) -> None:
    if resp.status_code == 429:
        raise ProbeFailure(
            f"{what} returned 429 RATE_LIMITED — the edge rate limiter interfered with the "
            f"probe. Body: {resp.text[:500]}"
        )
    if resp.status_code // 100 != 2:
        raise ProbeFailure(f"{what} returned {resp.status_code}. Body: {resp.text[:500]}")


async def register_users(client: httpx.AsyncClient, n: int, result: ProbeResult) -> list[User]:
    users: list[User] = []
    print(f"Phase A: registering {n} fresh users sequentially through /v1/auth/register ...")
    for i in range(n):
        email = f"probe-1029-{uuid.uuid4().hex[:12]}@studio.test"
        t0 = time.perf_counter()
        resp = await client.post(
            "/v1/auth/register",
            json={"email": email, "password": PROBE_PASSWORD, "full_name": f"Probe User {i}"},
            timeout=REQUEST_TIMEOUT_S,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        check_response(resp, f"register #{i} ({email})")
        result.register_latencies_ms.append(elapsed_ms)
        token = resp.json()["access_token"]
        users.append(User(email=email, password=PROBE_PASSWORD, token=token))
        print(f"  [{i + 1}/{n}] registered {email} in {elapsed_ms:.1f}ms")
        if i < n - 1:
            await asyncio.sleep(REGISTER_SPACING_S)
    return users


async def poll_me(client: httpx.AsyncClient, token: str) -> float:
    """One GET /v1/auth/me call. Returns elapsed ms. Raises ProbeFailure on non-2xx."""
    t0 = time.perf_counter()
    resp = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT_S,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    check_response(resp, "GET /v1/auth/me")
    return elapsed_ms


async def quiet_baseline(
    client: httpx.AsyncClient, token: str, count: int, result: ProbeResult
) -> None:
    print(f"\nPhase B: {count} sequential GET /v1/auth/me calls, nothing else in flight ...")
    for _ in range(count):
        elapsed_ms = await poll_me(client, token)
        result.quiet_latencies_ms.append(elapsed_ms)
    print(f"  done: {count} quiet samples collected")


async def background_poller(
    client: httpx.AsyncClient,
    token: str,
    interval_s: float,
    result: ProbeResult,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            elapsed_ms = await poll_me(client, token)
            result.load_latencies_ms.append(elapsed_ms)
        except ProbeFailure:
            raise
        except Exception as exc:  # noqa: BLE001 — a transient poller hiccup shouldn't kill the probe
            print(f"  [poller] transient error: {exc!r}", file=sys.stderr)
        await asyncio.sleep(interval_s)


async def do_login(client: httpx.AsyncClient, user: User, result: ProbeResult) -> None:
    t0 = time.perf_counter()
    resp = await client.post(
        "/v1/auth/login",
        json={"email": user.email, "password": user.password},
        timeout=REQUEST_TIMEOUT_S,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    check_response(resp, f"login ({user.email})")
    result.login_latencies_ms.append(elapsed_ms)


async def load_phase(
    client: httpx.AsyncClient, poll_token: str, users: list[User], result: ProbeResult
) -> None:
    print(
        f"\nPhase C: background /v1/auth/me poller every {LOAD_POLL_INTERVAL_S * 1000:.0f}ms "
        f"+ {len(users)} concurrent POST /v1/auth/login ..."
    )
    stop_event = asyncio.Event()
    poller_task = asyncio.create_task(
        background_poller(client, poll_token, LOAD_POLL_INTERVAL_S, result, stop_event)
    )
    try:
        # Let the poller get a couple of quiet samples in before the storm starts.
        await asyncio.sleep(LOAD_POLL_INTERVAL_S * 3)

        t0 = time.perf_counter()
        await asyncio.gather(*(do_login(client, u, result) for u in users))
        wall_ms = (time.perf_counter() - t0) * 1000
        print(f"  all {len(users)} concurrent logins finished in {wall_ms:.1f}ms wall time")

        # Give the poller a little tail so we can see recovery, then stop it.
        await asyncio.sleep(LOAD_POLL_INTERVAL_S * 5)
    finally:
        stop_event.set()
        await poller_task
    print(f"  collected {len(result.load_latencies_ms)} under-load /v1/auth/me samples")


def distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None, "mean": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))],
        "max": ordered[-1],
        "mean": statistics.fmean(ordered),
    }


def print_distribution(label: str, dist: dict) -> None:
    if dist["count"] == 0:
        print(f"{label}: no samples")
        return
    print(
        f"{label}: n={dist['count']:3d}  min={dist['min']:8.1f}ms  "
        f"median={dist['median']:8.1f}ms  p95={dist['p95']:8.1f}ms  "
        f"max={dist['max']:8.1f}ms  mean={dist['mean']:8.1f}ms"
    )


async def run(gateway: str) -> dict:
    result = ProbeResult()
    async with httpx.AsyncClient(base_url=gateway) as client:
        users = await register_users(client, NUM_USERS, result)
        baseline_user = users[0]

        await quiet_baseline(client, baseline_user.token, QUIET_POLL_COUNT, result)

        await load_phase(client, baseline_user.token, users, result)

    quiet_dist = distribution(result.quiet_latencies_ms)
    load_dist = distribution(result.load_latencies_ms)
    login_dist = distribution(result.login_latencies_ms)
    register_dist = distribution(result.register_latencies_ms)

    ratio_p95 = (
        (load_dist["p95"] / quiet_dist["p95"]) if quiet_dist["p95"] not in (None, 0) else None
    )

    print("\n" + "=" * 72)
    print("SUMMARY — issue #1029 auth concurrency probe")
    print("=" * 72)
    print_distribution("register (setup)          ", register_dist)
    print_distribution("GET /v1/auth/me  (quiet)   ", quiet_dist)
    print_distribution("GET /v1/auth/me  (under load)", load_dist)
    print_distribution("POST /v1/auth/login (concurrent)", login_dist)
    if ratio_p95 is not None:
        print(f"\nunder-load p95 / quiet p95 ratio: {ratio_p95:.2f}x")
    else:
        print("\nunder-load p95 / quiet p95 ratio: N/A (no quiet p95)")
    print("=" * 72)

    payload = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "gateway": gateway,
        "num_users": NUM_USERS,
        "quiet_poll_count": QUIET_POLL_COUNT,
        "load_poll_interval_s": LOAD_POLL_INTERVAL_S,
        "register_latencies_ms": result.register_latencies_ms,
        "quiet_latencies_ms": result.quiet_latencies_ms,
        "load_latencies_ms": result.load_latencies_ms,
        "login_latencies_ms": result.login_latencies_ms,
        "register_distribution": register_dist,
        "quiet_distribution": quiet_dist,
        "load_distribution": load_dist,
        "login_distribution": login_dist,
        "ratio_p95_load_over_quiet": ratio_p95,
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY, help="Application gateway base URL")
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Path to write the JSON result (required; parent directories are created)",
    )
    args = parser.parse_args()

    try:
        payload = asyncio.run(run(args.gateway))
    except ProbeFailure as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        sys.exit(1)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2))
    print(f"\nRaw JSON written to: {args.out}")


if __name__ == "__main__":
    main()
