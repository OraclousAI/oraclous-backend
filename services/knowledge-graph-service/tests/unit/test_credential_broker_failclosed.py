"""#653 — the KGS credential-broker mode must fail CLOSED, never silently fake.

The fake broker ignores the caller's ``credential_id`` and returns a hardcoded DSN pointing at the
platform's OWN Postgres — acceptable only as an EXPLICIT dev/CI opt-in. Today ``fake`` is the
silent default (``core/config.py``) and no shipped compose/helm config overrides it, so a real
user's credential is swapped for the platform DB with no error and no log. Same anti-pattern #295
already fixed for capability-registry (its default is ``"real"``); KGS has its own separate
``credential_client.py`` that fix never touched.

RED until the [impl] flips the default to ``"real"`` (an unset env then selects the REAL broker,
which fail-closes on missing base-url/key rather than silently faking).
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.services.credential_client import (
    CredentialResolutionError,
    FakeCredentialBroker,
    RealCredentialBroker,
    make_credential_broker,
)

pytestmark = pytest.mark.unit

_BROKER_ENV = (
    "KGS_CREDENTIAL_BROKER_MODE",
    "KGS_CREDENTIAL_BROKER_BASE_URL",
    "KGS_CREDENTIAL_BROKER_FAKE_DSN",
    "KGS_INTERNAL_SERVICE_KEY",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The issue's exact posture: NOTHING set — what every shipped compose file inherits."""
    for var in _BROKER_ENV:
        monkeypatch.delenv(var, raising=False)


def test_default_broker_mode_is_real_not_fake(clean_env) -> None:
    # The unsafe state must not be the default (#653 AC 1): with no env set at all, the mode is
    # "real" — the fake broker requires an explicit opt-in, exactly like CRS after #295.
    assert Settings().credential_broker_mode == "real"


def test_unset_mode_never_silently_selects_the_fake_broker(clean_env) -> None:
    # With nothing configured, building the broker must NOT hand back the fake (which would
    # ignore the user's credential_id). Real-without-config fail-closes with a loud
    # CredentialResolutionError instead — a 422 at ingest time, never a silent DSN swap.
    settings = Settings()
    try:
        broker = make_credential_broker(settings)
    except CredentialResolutionError:
        return  # fail-closed: acceptable (and expected with no base-url/key configured)
    assert not isinstance(broker, FakeCredentialBroker), (
        "an unconfigured deploy silently selected the FAKE credential broker — the user's "
        "credential_id would be ignored and the platform's own DB DSN used instead (#653)"
    )


def test_explicitly_configured_real_broker_is_selected(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("KGS_CREDENTIAL_BROKER_MODE", "real")
    monkeypatch.setenv("KGS_CREDENTIAL_BROKER_BASE_URL", "http://credential-broker-service:8000")
    monkeypatch.setenv("KGS_INTERNAL_SERVICE_KEY", "k")
    broker = make_credential_broker(Settings())
    assert isinstance(broker, RealCredentialBroker)


def test_fake_broker_still_available_as_an_explicit_opt_in(clean_env, monkeypatch) -> None:
    # The dev/CI seam survives — but only when the operator SAYS so.
    monkeypatch.setenv("KGS_CREDENTIAL_BROKER_MODE", "fake")
    broker = make_credential_broker(Settings())
    assert isinstance(broker, FakeCredentialBroker)


def test_shipped_deploy_configs_pin_the_broker_mode() -> None:
    # #653 AC 3: the compose stack and the production helm values must set the mode EXPLICITLY —
    # deployment posture is declared, never inherited from a code default. (The helm audit found
    # neither KGS web nor worker sets it today.)
    from pathlib import Path

    repo = Path(__file__).resolve().parents[4]
    compose = (repo / "deploy" / "docker-compose.yml").read_text()
    helm = (repo / "deploy" / "helm" / "values.yaml").read_text()
    assert "KGS_CREDENTIAL_BROKER_MODE" in compose, "compose never sets KGS_CREDENTIAL_BROKER_MODE"
    assert "KGS_CREDENTIAL_BROKER_MODE" in helm, "helm values never set KGS_CREDENTIAL_BROKER_MODE"
