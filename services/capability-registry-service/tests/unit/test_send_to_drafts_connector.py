"""Unit: the SendToDraftsConnector — the delivery sink that only DRAFTS (#489).

Decisive checks: a valid delivery becomes a DRAFT record (never SENT); an invalid/missing channel or
content is rejected; content is size-capped; the status is structurally always DRAFT; the plugin is
registered + factory-resolvable.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_capability_registry_service.domain.connectors.send_to_drafts import (
    SendToDraftsConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import create_executor
from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.domain.plugins.builtin import SendToDraftsPlugin

pytestmark = pytest.mark.unit


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
    )


def _ex() -> SendToDraftsConnector:
    return SendToDraftsConnector({"id": "x"})


async def test_a_valid_delivery_becomes_a_draft() -> None:
    res = await _ex().execute(
        {"channel": "email", "content": "the weekly digest", "recipient": "a@x.test"}, _ctx()
    )
    assert res.success
    assert res.data == {
        "status": "DRAFT",
        "channel": "email",
        "recipient": "a@x.test",
        "content": "the weekly digest",
    }
    assert res.metadata == {"sink": "drafts", "channel": "email"}


async def test_the_status_is_always_draft_never_sent() -> None:
    for channel in ("email", "slack", "notification", "webhook"):
        res = await _ex().execute({"channel": channel, "content": "x"}, _ctx())
        assert res.success and res.data["status"] == "DRAFT"


async def test_an_unknown_channel_is_rejected() -> None:
    res = await _ex().execute({"channel": "telepathy", "content": "x"}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_missing_content_is_rejected() -> None:
    res = await _ex().execute({"channel": "email"}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_non_string_content_is_rejected() -> None:
    res = await _ex().execute({"channel": "email", "content": 123}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_oversized_content_is_rejected() -> None:
    res = await _ex().execute({"channel": "email", "content": "a" * 100_001}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


def test_plugin_is_registered_and_factory_resolves_it() -> None:
    assert SendToDraftsPlugin in set(plugin_registry.discover())
    assert isinstance(create_executor(SendToDraftsPlugin.descriptor()), SendToDraftsConnector)
