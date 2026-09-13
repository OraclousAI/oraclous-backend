"""OpenAI-compatible client (slice 4): wire marshalling + tool_call parsing (mock transport)."""

from __future__ import annotations

import json

import httpx
import pytest
from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.llm.openai_compatible import (
    LLMClientError,
    OpenAICompatibleClient,
)

pytestmark = pytest.mark.unit

_SPEC = ToolSpec(
    name="pg__list_tables",
    description="list tables",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="pg",
    operation="list_tables",
)


def _client(handler) -> OpenAICompatibleClient:  # noqa: ANN001
    return OpenAICompatibleClient(
        base_url="https://router.test/api/v1",
        api_key="sk-test",
        model="vendor/model",
        transport=httpx.MockTransport(handler),
    )


async def test_emits_tools_and_parses_tool_calls() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-9",
                                    "type": "function",
                                    "function": {
                                        "name": "pg__list_tables",
                                        "arguments": '{"schema": "public"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    resp = await _client(handler).complete(
        messages=[{"role": "user", "content": "go"}], system="be helpful", tools=[_SPEC]
    )
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["model"] == "vendor/model"
    assert captured["body"]["messages"][0] == {"role": "system", "content": "be helpful"}
    assert captured["body"]["tools"][0]["function"]["name"] == "pg__list_tables"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "pg__list_tables"
    assert resp.tool_calls[0].args == {"schema": "public"}


async def test_parses_text_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "the answer"}}]})

    resp = await _client(handler).complete(
        messages=[{"role": "user", "content": "go"}], system="", tools=[]
    )
    assert resp.text == "the answer"
    assert resp.tool_calls == []


async def test_marshals_assistant_tool_calls_and_tool_results() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["msgs"] = json.loads(request.content)["messages"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "pg__list_tables", "args": {}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "pg__list_tables",
            "content": '{"tables": []}',
        },
    ]
    await _client(handler).complete(messages=messages, system="", tools=[_SPEC])
    msgs = captured["msgs"]
    assert msgs[1]["tool_calls"][0]["id"] == "c1"
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "pg__list_tables"
    assert msgs[2] == {"role": "tool", "tool_call_id": "c1", "content": '{"tables": []}'}


async def test_non_2xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    with pytest.raises(LLMClientError):
        await _client(handler).complete(
            messages=[{"role": "user", "content": "x"}], system="", tools=[]
        )


# ── ADR-042 (#551): the transient/permanent classification + leak-safety the retry depends on ──


async def _expect_error(status: int, body: str = "boom") -> LLMClientError:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    with pytest.raises(LLMClientError) as ei:
        await _client(handler).complete(
            messages=[{"role": "user", "content": "x"}], system="", tools=[]
        )
    return ei.value


@pytest.mark.parametrize(
    "status",
    [408, 409, 429, 500, 502, 503, 504, 529, 520, 522, 524, 527],  # incl. Cloudflare 52x (origin)
)
async def test_transient_statuses_are_classified_transient(status: int) -> None:
    exc = await _expect_error(status)
    assert exc.status_code == status
    assert exc.transient is True  # retried with backoff (any listed 4xx or any 5xx)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_statuses_are_classified_permanent(status: int) -> None:
    exc = await _expect_error(status)
    assert exc.status_code == status
    assert exc.transient is False  # auth/model-not-found/bad-request fail fast — never retried


async def test_a_timeout_is_transient() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LLMClientError) as ei:
        await _client(handler).complete(
            messages=[{"role": "user", "content": "x"}], system="", tools=[]
        )
    assert ei.value.transient is True  # a transport timeout is retryable


async def test_retry_after_header_is_parsed_onto_the_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down", headers={"Retry-After": "3"})

    with pytest.raises(LLMClientError) as ei:
        await _client(handler).complete(
            messages=[{"role": "user", "content": "x"}], system="", tools=[]
        )
    assert ei.value.retry_after == 3.0  # the server's wait hint is carried for the retry loop
    assert ei.value.transient is True


async def test_a_non_numeric_retry_after_is_ignored() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"})

    with pytest.raises(LLMClientError) as ei:
        await _client(handler).complete(
            messages=[{"role": "user", "content": "x"}], system="", tools=[]
        )
    assert ei.value.retry_after is None  # HTTP-date form → fall back to exponential backoff
    assert ei.value.transient is True


async def test_error_message_does_not_leak_the_upstream_body() -> None:
    # leak-safety (CLAUDE.md §11): the provider body may echo the customer's prompt/output, and this
    # message flows into the served team-run error_message — it must carry ONLY the coarse status.
    exc = await _expect_error(500, body="SECRET customer prompt echoed back")
    assert "SECRET" not in str(exc)
    assert "500" in str(exc)


# ── #252 — capture the prompt/completion (input/output) usage split ───────────────────────────────


async def test_parses_input_output_token_split() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                },
            },
        )

    resp = await _client(handler).complete(
        messages=[{"role": "user", "content": "go"}], system="", tools=[]
    )
    assert resp.total_tokens == 150
    assert resp.input_tokens == 120
    assert resp.output_tokens == 30


async def test_missing_split_keeps_total_and_zeroes_io() -> None:
    # a provider that reports only total_tokens (no prompt/completion) → split stays 0, total kept.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 77},
            },
        )

    resp = await _client(handler).complete(
        messages=[{"role": "user", "content": "go"}], system="", tools=[]
    )
    assert resp.total_tokens == 77
    assert resp.input_tokens == 0
    assert resp.output_tokens == 0


async def test_non_numeric_usage_does_not_fail_the_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": "x", "completion_tokens": None, "total_tokens": 10},
            },
        )

    resp = await _client(handler).complete(
        messages=[{"role": "user", "content": "go"}], system="", tools=[]
    )
    assert resp.total_tokens == 10
    assert resp.input_tokens == 0
    assert resp.output_tokens == 0


# ── #898: the wire carries the strict flag exactly when the spec says so ────────────────────────
#
# ``_SPEC`` above never sets ``strict`` (it predates #898 and defaults to ``False``); it is reused
# here as the "not strict" case so the two wire shapes are contrasted directly. ``ToolSpec`` has no
# ``strict`` field yet, so constructing one with ``strict=True`` is an ordinary keyword call to an
# EXISTING dataclass that fails with ``TypeError`` at test runtime (not a not-yet-built intra-repo
# seam import — see ``.claude/rules/tests-seam-imports.md``), same posture as every ``nullable_keys=``
# construction in ``test_dispatch_payload_null_strip.py``.


async def test_a_strict_spec_sends_the_flag_inside_the_function_object() -> None:
    strict_spec = ToolSpec(
        name="knowledge-retriever__search",
        description="Search a knowledge graph",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        binding="knowledge-retriever",
        operation="search",
        strict=True,
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    await _client(handler).complete(
        messages=[{"role": "user", "content": "go"}], system="", tools=[strict_spec]
    )
    assert captured["body"]["tools"][0]["function"]["strict"] is True


async def test_a_non_strict_spec_sends_no_strict_key_at_all() -> None:
    """Absent, never ``false`` — the two must be distinguishable on the wire."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    await _client(handler).complete(
        messages=[{"role": "user", "content": "go"}], system="", tools=[_SPEC]
    )
    assert "strict" not in captured["body"]["tools"][0]["function"]


async def test_an_imported_mcp_spec_never_carries_the_strict_key() -> None:
    """An MCP-imported spec's ``strict`` is always ``False`` (never inferred — #898/#900), so its
    wire shape must be indistinguishable from any other non-strict tool."""
    mcp_spec = ToolSpec(
        name="github-mcp__pull_request_read",
        description="Read a pull request",
        parameters={"type": "object", "properties": {"repo": {"type": "string"}}},
        binding="github-mcp",
        operation="pull_request_read",
        strict=False,
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    await _client(handler).complete(
        messages=[{"role": "user", "content": "go"}], system="", tools=[mcp_spec]
    )
    assert "strict" not in captured["body"]["tools"][0]["function"]
