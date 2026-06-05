"""Unit: aggregated-health rollup logic (ok / degraded / down)."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.services.health_service import HealthService

pytestmark = pytest.mark.unit


class _FakeUpstreamClient:
    def __init__(self, codes: dict[str, int | None]) -> None:
        self._codes = codes

    async def health_check(self, base_url: str, *, timeout_s: float) -> int | None:  # noqa: ARG002
        return self._codes.get(base_url)


_TARGETS = {"auth": "http://auth:8000", "krs": "http://krs:8000", "capreg": "http://capreg:8000"}


async def test_all_ok_rolls_up_to_ok() -> None:
    svc = HealthService(
        upstream_client=_FakeUpstreamClient(dict.fromkeys(_TARGETS.values(), 200)),
        targets=_TARGETS,
    )
    out = await svc.check_all()
    assert out.overall == "ok"
    assert {u.name for u in out.upstreams} == {"auth", "krs", "capreg"}
    assert all(u.status == "ok" for u in out.upstreams)


async def test_one_down_rolls_up_to_degraded() -> None:
    codes = {"http://auth:8000": 200, "http://krs:8000": None, "http://capreg:8000": 200}
    svc = HealthService(upstream_client=_FakeUpstreamClient(codes), targets=_TARGETS)
    out = await svc.check_all()
    assert out.overall == "degraded"
    krs = next(u for u in out.upstreams if u.name == "krs")
    assert krs.status == "down"


async def test_non_200_is_degraded() -> None:
    codes = {"http://auth:8000": 503, "http://krs:8000": 200, "http://capreg:8000": 200}
    svc = HealthService(upstream_client=_FakeUpstreamClient(codes), targets=_TARGETS)
    out = await svc.check_all()
    assert out.overall == "degraded"
    assert next(u for u in out.upstreams if u.name == "auth").status == "degraded"


async def test_results_are_order_stable_by_name() -> None:
    svc = HealthService(
        upstream_client=_FakeUpstreamClient(dict.fromkeys(_TARGETS.values(), 200)),
        targets=_TARGETS,
    )
    names = [u.name for u in (await svc.check_all()).upstreams]
    assert names == sorted(names)
