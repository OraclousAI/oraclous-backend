"""Tests for the BYOM-model-key guardrail (#724 / ADR-008 §3.6).

A model call over customer data must resolve the ORG's credential from the credential-broker. The
failure mode is not a wrong call at runtime, it is a **deployment** that hands a service a model key
of the platform's: once the env var is there, any code path can reach it, and the customer's data is
read on a key they neither chose nor pay for.

The guardrail denies a platform model key reaching a service two ways:

  BMK001 — a deployment manifest injects a model API key into a service environment, e.g.
           ``KGS_OPENAI_API_KEY: "${OPENROUTER_API_KEY}"`` in compose, or the same key in a Helm
           values env block. Infrastructure secrets (``INTERNAL_SERVICE_KEY``, ``AUTH_JWT_SECRET``,
           ``OAUTH_*``, encryption keys) are NOT model keys and are exempt: they are Oraclous's own
           identity and crypto material, not a model reading customer data.
  BMK002 — a service ``core/config.py`` declares a model-key field (``openai_api_key``,
           ``anthropic_api_key``, and the like), which is the thing a deployment would populate.

Test-runner env is explicitly not in scope. ``OPENROUTER_API_KEY`` and ``TAVILY_API_KEY`` read from
``deploy/.env`` by an e2e test are BYOM *sources*: the test pastes them through the credentials API
so they become an org credential, which is the pattern this guardrail exists to protect. The rule is
about what a **service container** is handed, not what a test harness reads.

RED until the [impl] lands ``tools/lint/check_byom_model_keys.py``.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _check_manifest(src: str) -> set[str]:
    """The not-yet-built guardrail, imported function-locally so collection stays green and this
    hard-FAILS rather than skipping while it is missing (CLAUDE.md §4.1)."""
    from tools.lint.check_byom_model_keys import check_manifest  # noqa: PLC0415

    return {v.rule for v in check_manifest(src)}


def _check_config(src: str) -> set[str]:
    from tools.lint.check_byom_model_keys import check_config_source  # noqa: PLC0415

    return {v.rule for v in check_config_source(src)}


# --- BMK001: a deployment manifest hands a service a model key ------------------------------


def test_compose_injecting_a_model_key_is_denied() -> None:
    # The exact shape at deploy/docker-compose.yml:63 that this issue is about.
    assert "BMK001" in _check_manifest('  KGS_OPENAI_API_KEY: "${OPENROUTER_API_KEY}"')


def test_a_nested_default_does_not_hide_it() -> None:
    # deploy/docker-compose.yml:421 — the fallback chain still ends at a platform key.
    assert "BMK001" in _check_manifest(
        '  KRS_OPENAI_API_KEY: "${KRS_OPENAI_API_KEY:-${OPENROUTER_API_KEY:-}}"'
    )


def test_a_helm_values_env_entry_is_denied() -> None:
    # deploy/helm/values-prod.example.yaml:159 — the production path, same key, different file.
    assert "BMK001" in _check_manifest(
        '        KGS_OPENAI_API_KEY:       { secretName: "openrouter-key", secretKey: "value" }'
    )


@pytest.mark.parametrize(
    "line",
    [
        '  INTERNAL_SERVICE_KEY: "${INTERNAL_SERVICE_KEY}"',
        '  JWT_SECRET: "${AUTH_JWT_SECRET}"',
        '  OAUTH_GOOGLE_CLIENT_SECRET: "${OAUTH_GOOGLE_CLIENT_SECRET}"',
        '  ENCRYPTION_KEY: "${CRED_BROKER_ENCRYPTION_KEY}"',
        '  OAUTH_ENC_KEY: "${OAUTH_ENC_KEY}"',
    ],
)
def test_infrastructure_secrets_are_not_model_keys(line: str) -> None:
    """Oraclous's own identity and crypto material. Platform-owned is correct for these, and a
    guardrail that fired on them would be turned off within a week."""
    assert _check_manifest(line) == set()


def test_a_tavily_key_in_a_service_env_is_still_denied() -> None:
    """Provider-agnostic on purpose. Tavily is BYOM today (web_research resolves it from the
    execution context), and a deployment putting it back into a service env would undo that."""
    assert "BMK001" in _check_manifest('  CRS_TAVILY_API_KEY: "${TAVILY_API_KEY}"')


@pytest.mark.parametrize("line", ["TAVILY_API_KEY=tvly-xxx", "OPENROUTER_API_KEY=sk-or-v1-xxx"])
def test_an_env_file_source_is_not_a_service_env(line: str) -> None:
    """The exemption the guardrail depends on, pinned rather than only documented.

    ``deploy/.env`` legitimately holds these: an e2e reads one and pastes it through the
    credentials API so it becomes an org credential, which is the pattern #724 protects. A
    guardrail that fired on the env file would flag the correct BYOM flow and be switched off.

    The tell is the shape. ``KEY=value`` is an env-file assignment; ``KEY: "${...}"`` under a
    service is an injection into a container.
    """
    assert _check_manifest(line) == set()


# --- BMK002: a service config declares the field a deployment would populate -----------------


def test_a_model_key_field_on_service_config_is_denied() -> None:
    assert "BMK002" in _check_config(
        "class Settings(BaseSettings):\n    openai_api_key: str | None = None\n"
    )


def test_a_non_model_secret_field_is_allowed() -> None:
    """``check_failclosed_secrets`` already owns the shape of these; this guardrail is only about
    whose MODEL key a service holds."""
    assert (
        _check_config("class Settings(BaseSettings):\n    internal_service_key: str = ''\n")
        == set()
    )


def test_a_broker_resolved_credential_id_is_allowed() -> None:
    """The replacement shape must pass: an id is not a key, and pointing at a credential is
    exactly what #724 asks for."""
    assert (
        _check_config(
            "class Settings(BaseSettings):\n    default_model_credential_id: str | None = None\n"
        )
        == set()
    )
