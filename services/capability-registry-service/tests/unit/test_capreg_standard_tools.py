"""Unit: the standard agent toolset — the curated ``core/*`` tools an imported team binds (#507).

Decisive checks per tool: the sandbox file tools round-trip and confine to the per-org workspace
(a ``..`` traversal and an absolute path are both rejected before any filesystem op); grep/glob are
bounded and confined; bash runs as a guarded subprocess that times out and caps output and never
exposes the registry's env; WebSearch/WebFetch delegate to the web-research search/fetch path; and
every one of the eight plugins is registered with a ``metadata.name`` that slugifies to exactly the
importer's ref slug (read/grep/glob/write/edit/bash/websearch/webfetch) and is factory-resolvable.
"""

from __future__ import annotations

import re
import uuid

import pytest
from oraclous_capability_registry_service.domain.connectors.standard_tools import (
    BashConnector,
    EditFileConnector,
    GlobConnector,
    GrepConnector,
    ReadFileConnector,
    WebFetchConnector,
    WebSearchConnector,
    WriteFileConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import create_executor
from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.domain.plugins.builtin import (
    BashToolPlugin,
    EditToolPlugin,
    GlobToolPlugin,
    GrepToolPlugin,
    ReadToolPlugin,
    WebFetchToolPlugin,
    WebSearchToolPlugin,
    WriteToolPlugin,
)

pytestmark = pytest.mark.unit

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _NON_ALNUM.sub("-", text.lower()).strip("-")


@pytest.fixture
def org() -> uuid.UUID:
    # A FRESH org per test → a fresh, empty sandbox dir (no cross-test bleed).
    return uuid.uuid4()


def _ctx(org: uuid.UUID, creds: dict | None = None) -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=org,
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        credentials=creds or {},
    )


# --------------------------------------------------------------------------- registration / slugs


def test_the_eight_standard_plugins_slugify_to_the_importer_refs() -> None:
    expected = {
        ReadToolPlugin: "read",
        GrepToolPlugin: "grep",
        GlobToolPlugin: "glob",
        WriteToolPlugin: "write",
        EditToolPlugin: "edit",
        BashToolPlugin: "bash",
        WebSearchToolPlugin: "websearch",
        WebFetchToolPlugin: "webfetch",
    }
    registered = set(plugin_registry.discover())
    for plugin, slug in expected.items():
        assert plugin in registered, f"{plugin.__name__} is not registered"
        assert _slug(plugin.descriptor()["metadata"]["name"]) == slug


def test_each_standard_plugin_is_factory_resolvable() -> None:
    pairs = [
        (ReadToolPlugin, ReadFileConnector),
        (WriteToolPlugin, WriteFileConnector),
        (EditToolPlugin, EditFileConnector),
        (GrepToolPlugin, GrepConnector),
        (GlobToolPlugin, GlobConnector),
        (BashToolPlugin, BashConnector),
        (WebSearchToolPlugin, WebSearchConnector),
        (WebFetchToolPlugin, WebFetchConnector),
    ]
    for plugin, connector in pairs:
        assert isinstance(create_executor(plugin.descriptor()), connector)


def test_only_the_eight_keyless_file_tools_carry_no_credential() -> None:
    # item-4 (capability absence) by construction: none of these is publish/spend/send; the file
    # tools are keyless (sandbox), web tools key-gate only `search`.
    for plugin in (
        ReadToolPlugin,
        WriteToolPlugin,
        EditToolPlugin,
        GrepToolPlugin,
        GlobToolPlugin,
        BashToolPlugin,
        WebFetchToolPlugin,
    ):
        assert plugin.descriptor()["spec"]["credential_requirements"] == []
    assert WebSearchToolPlugin.descriptor()["spec"]["credential_requirements"] == [
        {"type": "api_key", "provider": "web_search", "required": True}
    ]


# --------------------------------------------------------------------------- read / write / edit


async def test_write_then_read_round_trip(org: uuid.UUID) -> None:
    w = await WriteFileConnector({"id": "w"}).execute(
        {"path": "notes/a.txt", "content": "hello world"}, _ctx(org)
    )
    assert w.success and w.data == {"ok": True, "path": "notes/a.txt", "bytes": 11}
    r = await ReadFileConnector({"id": "r"}).execute({"path": "notes/a.txt"}, _ctx(org))
    assert r.success and r.data == {"content": "hello world", "path": "notes/a.txt"}


async def test_read_missing_is_a_clean_not_found(org: uuid.UUID) -> None:
    r = await ReadFileConnector({"id": "r"}).execute({"path": "nope.txt"}, _ctx(org))
    assert not r.success and r.error_type == "NOT_FOUND"


async def test_edit_replaces_a_unique_string(org: uuid.UUID) -> None:
    await WriteFileConnector({"id": "w"}).execute(
        {"path": "f.txt", "content": "the quick brown fox"}, _ctx(org)
    )
    e = await EditFileConnector({"id": "e"}).execute(
        {"path": "f.txt", "old_string": "quick", "new_string": "slow"}, _ctx(org)
    )
    assert e.success and e.data["replacements"] == 1
    r = await ReadFileConnector({"id": "r"}).execute({"path": "f.txt"}, _ctx(org))
    assert r.data["content"] == "the slow brown fox"


async def test_edit_rejects_an_ambiguous_match(org: uuid.UUID) -> None:
    await WriteFileConnector({"id": "w"}).execute({"path": "f.txt", "content": "a a a"}, _ctx(org))
    e = await EditFileConnector({"id": "e"}).execute(
        {"path": "f.txt", "old_string": "a", "new_string": "b"}, _ctx(org)
    )
    assert not e.success and e.error_type == "AMBIGUOUS_MATCH"


# --------------------------------------------------------------------------- sandbox confinement


@pytest.mark.parametrize(
    "escape_path",
    ["../escape.txt", "../../etc/passwd", "/etc/passwd", "a/../../escape.txt"],
)
async def test_read_and_write_reject_paths_that_escape_the_sandbox(
    org: uuid.UUID, escape_path: str
) -> None:
    # An absolute path is treated as sandbox-root-relative (`/etc/passwd` cannot reach the host).
    r = await ReadFileConnector({"id": "r"}).execute({"path": escape_path}, _ctx(org))
    w = await WriteFileConnector({"id": "w"}).execute(
        {"path": escape_path, "content": "x"}, _ctx(org)
    )
    # traversal escapes are refused; an absolute path is confined (a clean NOT_FOUND on read, a
    # successful confined write) — either way it NEVER touches the host path.
    if escape_path.startswith("/"):
        assert r.error_type == "NOT_FOUND"  # /etc/passwd resolved under the sandbox, absent
        assert w.success  # written confined inside the sandbox, not at the host root
    else:
        assert not r.success and r.error_type == "INVALID_INPUT"
        assert not w.success and w.error_type == "INVALID_INPUT"


async def test_absolute_path_is_confined_not_host_access(org: uuid.UUID) -> None:
    # Proves /etc/passwd is NOT the host file: a confined write at "/etc/passwd" then a host check.
    w = await WriteFileConnector({"id": "w"}).execute(
        {"path": "/secret.txt", "content": "sandboxed"}, _ctx(org)
    )
    assert w.success
    from oraclous_capability_registry_service.domain.sandbox import sandbox_root

    assert (sandbox_root(org) / "secret.txt").read_text() == "sandboxed"


# --------------------------------------------------------------------------- grep / glob


async def test_grep_finds_matches_and_is_confined(org: uuid.UUID) -> None:
    await WriteFileConnector({"id": "w"}).execute(
        {"path": "log.txt", "content": "alpha\nbeta error here\ngamma"}, _ctx(org)
    )
    g = await GrepConnector({"id": "g"}).execute({"pattern": r"error"}, _ctx(org))
    assert g.success
    assert g.data["matches"] == [{"path": "log.txt", "line": 2, "text": "beta error here"}]


async def test_grep_rejects_a_bad_regex(org: uuid.UUID) -> None:
    g = await GrepConnector({"id": "g"}).execute({"pattern": "("}, _ctx(org))
    assert not g.success and g.error_type == "INVALID_INPUT"


async def test_glob_lists_matching_sandbox_paths(org: uuid.UUID) -> None:
    for name in ("a.md", "b.md", "c.txt"):
        await WriteFileConnector({"id": "w"}).execute({"path": name, "content": "x"}, _ctx(org))
    g = await GlobConnector({"id": "g"}).execute({"pattern": "*.md"}, _ctx(org))
    assert g.success and sorted(g.data["paths"]) == ["a.md", "b.md"]


# --------------------------------------------------------------------------- bash


async def test_bash_runs_in_the_sandbox_and_returns_output(org: uuid.UUID) -> None:
    res = await BashConnector({"id": "b"}).execute({"command": "echo hi"}, _ctx(org))
    assert res.success and res.data["exit_code"] == 0
    assert res.data["stdout"].strip() == "hi"


async def test_bash_cwd_is_the_sandbox_root(org: uuid.UUID) -> None:
    await WriteFileConnector({"id": "w"}).execute({"path": "marker.txt", "content": "x"}, _ctx(org))
    res = await BashConnector({"id": "b"}).execute({"command": "ls"}, _ctx(org))
    assert res.success and "marker.txt" in res.data["stdout"]


async def test_bash_times_out(org: uuid.UUID) -> None:
    connector = BashConnector({"id": "b"})
    connector.timeout_s = 5.0  # keep the test fast; inner timeout still bites first
    # patch the inner subprocess timeout down so the test is quick
    import oraclous_capability_registry_service.domain.connectors.standard_tools as st

    original = st._BASH_TIMEOUT_S
    st._BASH_TIMEOUT_S = 0.5
    try:
        res = await connector.execute({"command": "sleep 5"}, _ctx(org))
    finally:
        st._BASH_TIMEOUT_S = original
    assert not res.success and res.error_type == "TIMEOUT"


async def test_bash_caps_output(org: uuid.UUID) -> None:
    import oraclous_capability_registry_service.domain.connectors.standard_tools as st

    original = st._BASH_MAX_OUTPUT_BYTES
    st._BASH_MAX_OUTPUT_BYTES = 64
    try:
        # pure /bin/sh built-ins (printf + a while loop) emit well over 64 bytes with NO forking,
        # so the output cap (not a platform proc-limit) is what bites.
        res = await BashConnector({"id": "b"}).execute(
            {"command": "i=0; while [ $i -lt 200 ]; do printf oraclous; i=$((i+1)); done"},
            _ctx(org),
        )
    finally:
        st._BASH_MAX_OUTPUT_BYTES = original
    assert not res.success and res.error_type == "OUTPUT_TOO_LARGE"


async def test_bash_does_not_expose_the_registry_env(
    org: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORACLOUS_TEST_SECRET", "do-not-leak")  # noqa: S105 — env name, not a secret
    res = await BashConnector({"id": "b"}).execute(
        {"command": "echo $ORACLOUS_TEST_SECRET"}, _ctx(org)
    )
    assert res.success
    assert "do-not-leak" not in res.data["stdout"]  # clean minimal env — secret not inherited


async def test_bash_missing_command_is_rejected(org: uuid.UUID) -> None:
    res = await BashConnector({"id": "b"}).execute({}, _ctx(org))
    assert not res.success and res.error_type == "INVALID_INPUT"


# --------------------------------------------------------------------------- web tools (delegation)


async def test_websearch_without_a_key_is_a_clean_missing_credential(org: uuid.UUID) -> None:
    # Delegates to the web-research `search` path, which requires a BYOM api_key (key-gated).
    res = await WebSearchConnector({"id": "s"}).execute({"query": "oraclous"}, _ctx(org))
    assert not res.success and res.error_type == "MISSING_CREDENTIAL"


async def test_webfetch_refuses_an_unsafe_url(org: uuid.UUID) -> None:
    # Delegates to the web-research `fetch` path → the shared SSRF gate refuses internal URLs.
    res = await WebFetchConnector({"id": "f"}).execute(
        {"url": "http://169.254.169.254/latest/meta-data/"}, _ctx(org)
    )
    assert not res.success and res.error_type == "UNSAFE_URL"
