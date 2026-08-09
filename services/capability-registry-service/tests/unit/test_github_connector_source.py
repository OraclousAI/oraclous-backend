"""The GitHub connector emits ``source`` from what it already holds (#742, criterion 10).

Today ``read_file`` returns exactly ``{"path": ..., "content": ...}``. The GitHub Contents API
response it just parsed carries ``sha`` and ``html_url``, and both are discarded on that line —
which is why a knowledge record ends up with ``ingestion_source = "README.md"`` and no way to
resolve it. **No new network call is needed**: every field below is already in the response.

The connector is the one party holding source-native identity, so it is the one party that may
fill ``source``. The ingest caller passes it through unmodified and never synthesises one.

Only ``read_file`` gains this. ``list_files`` returns entries, not content, so there is nothing to
cite; no other connector changes in this slice.

The connector module is already built, so it is imported normally; there is no ``oraclous_citation``
seam on this path — the connector emits a plain source mapping and the platform mints from it.

RED until backend-implementer adds ``source`` to ``GitHubReader.read_file``.
"""

from __future__ import annotations

import base64
import uuid

import httpx
import pytest
from oraclous_capability_registry_service.domain.connectors.github import GitHubReader
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit

_README = "# oraclous-backend\n\nThe platform's Python codebase.\n"
_BLOB_SHA = "9f2a4c81b3d7e5061c8a4f2b6d90e37c15ab8e42"
_HTML_URL = f"https://github.com/OraclousAI/oraclous-backend/blob/{_BLOB_SHA}/docs/README.md"

#: A real GitHub Contents API response for a file, trimmed to the fields that matter here.
_CONTENTS_RESPONSE = {
    "name": "README.md",
    "path": "docs/README.md",
    "sha": _BLOB_SHA,
    "size": len(_README),
    "html_url": _HTML_URL,
    "type": "file",
    "encoding": "base64",
    "content": base64.b64encode(_README.encode("utf-8")).decode("ascii"),
}


def _context() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        credentials={"api_key": {"api_key": "ghp_test_token"}},
    )


async def _read_file(payload: object, *, repo: str = "OraclousAI/oraclous-backend"):
    reader = GitHubReader({"id": "github-reader"})
    reader.transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    return await reader._execute_internal(
        {"operation": "read_file", "repo": repo, "path": "docs/README.md"}, _context()
    )


class TestReadFileEmitsSource:
    async def test_the_content_is_still_returned_unchanged(self) -> None:
        """The existing contract is additive — ``source`` is a third key, not a replacement."""
        result = await _read_file(_CONTENTS_RESPONSE)

        assert result.success is True
        assert result.data["path"] == "docs/README.md"
        assert result.data["content"] == _README

    async def test_revision_is_the_blob_sha_from_the_same_response(self) -> None:
        result = await _read_file(_CONTENTS_RESPONSE)

        assert result.data["source"]["revision"] == _BLOB_SHA
        assert result.data["source"]["revision_kind"] == "blob_sha"

    async def test_url_is_the_html_url_from_the_same_response(self) -> None:
        """ "Resolvable" means a person can open it. The API already hands us the permalink."""
        result = await _read_file(_CONTENTS_RESPONSE)

        assert result.data["source"]["url"] == _HTML_URL

    async def test_source_id_is_the_repo_and_path(self) -> None:
        """Identity WITHIN github, stable across revisions — the repo plus the file path, so two
        files of the same name in different repositories are never the same document."""
        result = await _read_file(_CONTENTS_RESPONSE)

        assert result.data["source"]["source_system"] == "github"
        assert result.data["source"]["source_id"] == "OraclousAI/oraclous-backend:docs/README.md"

    async def test_title_is_the_basename_of_the_path(self) -> None:
        """A display label only — the path is the identity, the basename is what a reader sees."""
        result = await _read_file(_CONTENTS_RESPONSE)

        assert result.data["source"]["title"] == "README.md"

    async def test_author_is_null_because_the_contents_api_carries_none(self) -> None:
        """The Contents API response has no author field. Fetching one costs a second API call per
        read, which belongs with the source object (#740) — so this slice records the gap honestly
        rather than inventing a name."""
        result = await _read_file(_CONTENTS_RESPONSE)

        assert result.data["source"]["author"] is None

    async def test_permission_ref_is_carried_as_null(self) -> None:
        """Carried, unenforced (#737) — in the shape now so records written today are still
        usable when per-user permission mirroring lands."""
        result = await _read_file(_CONTENTS_RESPONSE)

        assert result.data["source"]["permission_ref"] is None

    async def test_the_source_carries_no_platform_computed_field(self) -> None:
        """A connector never mints. ``citation_id`` and ``retrieved_at`` are the platform's."""
        source = (await _read_file(_CONTENTS_RESPONSE)).data["source"]

        assert "citation_id" not in source
        assert "retrieved_at" not in source

    async def test_the_emitted_source_is_a_valid_source_ref(self) -> None:
        """What the connector emits is exactly what the ingest boundary accepts — otherwise the
        pass-it-through-unmodified rule cannot hold."""
        from oraclous_citation import SourceRef  # RED

        source = (await _read_file(_CONTENTS_RESPONSE)).data["source"]

        assert SourceRef(**source).source_id == "OraclousAI/oraclous-backend:docs/README.md"


class TestListFilesIsUnchanged:
    async def test_list_files_emits_no_source(self) -> None:
        """It returns directory entries, not content, so there is nothing to cite."""
        reader = GitHubReader({"id": "github-reader"})
        reader.transport = httpx.MockTransport(
            lambda _: httpx.Response(200, json=[{"name": "README.md", "type": "file"}])
        )

        result = await reader._execute_internal(
            {"operation": "list_files", "repo": "OraclousAI/oraclous-backend"}, _context()
        )

        assert result.success is True
        assert "source" not in result.data
        assert result.data["entries"][0]["name"] == "README.md"
