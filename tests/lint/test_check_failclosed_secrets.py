"""Tests for the fail-closed-secrets guardrail (WP-1, T6 / ADR-008).

Verifies the checker fires on a baked-in secret default (literal field assignment or
``os.environ.get(name, "<literal>")``) and passes the fixed, require_secret-based config shape.
"""

from __future__ import annotations

import pytest
from tools.lint.check_failclosed_secrets import check_source

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _rules(src: str) -> set[str]:
    return {v.rule for v in check_source(src)}


# --- FCS001: string-literal default on a secret field -----------------------


def test_fcs001_internal_service_key_literal_default() -> None:
    src = 'class S:\n    INTERNAL_SERVICE_KEY: str = "dev-internal-key"\n'
    assert "FCS001" in _rules(src)


def test_fcs001_jwt_secret_literal_default_plain_assign() -> None:
    src = 'jwt_secret = "change-me-in-production"\n'
    assert "FCS001" in _rules(src)


def test_fcs001_suffix_secret_field() -> None:
    src = 'class S:\n    webhook_signing_secret: str = "hunter2"\n'
    assert "FCS001" in _rules(src)


def test_fcs001_suffix_api_key_field() -> None:
    src = 'class S:\n    openai_api_key: str = "sk-baked-in"\n'
    assert "FCS001" in _rules(src)


def test_fcs001_empty_string_default_is_allowed() -> None:
    src = 'class S:\n    INTERNAL_SERVICE_KEY: str = ""\n'
    assert "FCS001" not in _rules(src)


def test_fcs001_none_default_is_allowed() -> None:
    src = "class S:\n    JWT_SECRET: str | None = None\n"
    assert "FCS001" not in _rules(src)


def test_fcs001_no_default_field_is_allowed() -> None:
    src = "class S:\n    INTERNAL_SERVICE_KEY: str\n"
    assert "FCS001" not in _rules(src)


def test_fcs001_dev_fallback_constant_is_exempt() -> None:
    # The gated dev-fallback constants that feed require_secret are NOT secret fields.
    src = (
        '_DEV_JWT_SECRET = "change-me-in-production"\n'
        '_DEV_INTERNAL_SERVICE_KEY = "dev-internal-key"\n'
        '_DEV_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="\n'
    )
    assert _rules(src) == set()


# --- FCS002: os.environ.get(name, "<literal>") for a secret name ------------


def test_fcs002_environ_get_with_literal_default() -> None:
    src = 'import os\nx = os.environ.get("JWT_SECRET", "change-me-in-production")\n'
    assert "FCS002" in _rules(src)


def test_fcs002_getenv_with_literal_default() -> None:
    src = 'import os\nx = os.getenv("INTERNAL_SERVICE_KEY", "dev-internal-key")\n'
    assert "FCS002" in _rules(src)


def test_fcs002_environ_get_empty_default_allowed() -> None:
    src = 'import os\nx = os.environ.get("JWT_SECRET", "")\n'
    assert "FCS002" not in _rules(src)


def test_fcs002_environ_get_no_default_allowed() -> None:
    src = 'import os\nx = os.environ.get("JWT_SECRET")\n'
    assert "FCS002" not in _rules(src)


def test_fcs002_non_secret_env_var_allowed() -> None:
    src = 'import os\nx = os.environ.get("JWT_ALGORITHM", "HS256")\n'
    assert _rules(src) == set()


# --- the require_secret pattern is clean ------------------------------------


def test_require_secret_pattern_passes() -> None:
    src = (
        "from oraclous_governance import require_secret\n"
        '_DEV_KEY = "dev-fallback"  # noqa\n'
        'jwt_secret = require_secret("JWT_SECRET", dev_default=_DEV_KEY)\n'
    )
    assert _rules(src) == set()
