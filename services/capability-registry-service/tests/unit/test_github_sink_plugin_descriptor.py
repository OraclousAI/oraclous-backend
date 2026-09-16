"""Unit: the `GitHubSinkPlugin` descriptor after #1047 — `repo` is operator-configured only.

Owner ruling (#1047, 16 Sep, Q2): a call-supplied `repo` is never legitimate on its own — the
standard confused-deputy defence (same shape as the graph id in #524, the vendor argument in #946
D1). So `repo` leaves the model-facing `deliver` operation's advertised parameters entirely,
`CONFIGURATION_SCHEMA` gains it as a required instance-configuration field, and
`INPUT_SCHEMA.required` drops it (an explicit call-supplied `repo` is still tolerated when it
matches the configured one — see test_github_sink_connector.py — but never demanded of, or
advertised to, the model).

RED until the #1047 [impl] lands.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_deliver_operation_no_longer_advertises_repo_as_a_call_parameter() -> None:
    from oraclous_capability_registry_service.domain.plugins.builtin import GitHubSinkPlugin

    deliver = next(c for c in GitHubSinkPlugin.CAPABILITIES if c["name"] == "deliver")
    assert "repo" not in deliver["parameters"]


def test_configuration_schema_declares_repo_as_a_required_string() -> None:
    from oraclous_capability_registry_service.domain.plugins.builtin import GitHubSinkPlugin

    schema = GitHubSinkPlugin.CONFIGURATION_SCHEMA
    assert schema.get("properties", {}).get("repo", {}).get("type") == "string"
    assert "repo" in (schema.get("required") or [])


def test_input_schema_no_longer_requires_repo() -> None:
    from oraclous_capability_registry_service.domain.plugins.builtin import GitHubSinkPlugin

    assert "repo" not in (GitHubSinkPlugin.INPUT_SCHEMA.get("required") or [])
