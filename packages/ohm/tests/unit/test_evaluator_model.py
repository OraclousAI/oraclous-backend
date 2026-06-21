"""OHMManifest.evaluator_model() — the BYOM judge-model resolver (ADR-037 / BYOM-judge).

A team declares its flow-evaluation judge as a models[] entry with role="evaluator" carrying
config.credential_id (mirrors the role="primary" BYOM model). Unlike primary_model() there is NO
models[0] fallback — absence returns None so the gate uses the operator key, never mis-grades with
the first model's credential."""

from __future__ import annotations

import pytest
from oraclous_ohm.manifest import OHMManifest, OHMMetadata, OHMModel, OHMRuntime

pytestmark = [pytest.mark.unit]


def _manifest(models: list[OHMModel]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(
            id="00000000-0000-0000-0000-000000000001",
            name="t",
            owner_organization_id="00000000-0000-0000-0000-000000000002",
            kind="team",
        ),
        runtime=OHMRuntime(entrypoint="primary"),
        models=models,
    )


def _model(role: str, *, credential_id: str | None = None) -> OHMModel:
    cfg = {"credential_id": credential_id} if credential_id else {}
    return OHMModel(
        role=role,
        binding="openrouter/openai/gpt-4o-mini",
        protocol_shape="openai-compatible",
        config=cfg,
    )


def test_evaluator_model_resolves_the_role() -> None:
    m = _manifest([_model("primary"), _model("evaluator", credential_id="cred-123")])
    ev = m.evaluator_model()
    assert ev is not None and ev.config["credential_id"] == "cred-123"
    assert ev.binding == "openrouter/openai/gpt-4o-mini"


def test_evaluator_model_absent_returns_none_no_fallback() -> None:
    # NO models[0] fallback (unlike primary_model) — absence cleanly returns None
    m = _manifest([_model("primary", credential_id="cred-primary")])
    assert m.evaluator_model() is None
    assert m.primary_model() is not None  # primary still resolves
