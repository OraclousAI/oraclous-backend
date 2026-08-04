"""The stored NAME shape of an imported MCP tool descriptor (#698 D4).

Two matching rules read the same capability reference and split it differently. The runtime's
``_ref_slug`` keeps the tail after the LAST ``/``; its policy check ``_registry_of`` keeps the head
before the FIRST ``/``. A descriptor stored as ``"<label>/<tool>"`` can only be named by the ref
``org:<id>/<label>/<tool>``, which resolves to the slug ``<tool>`` and matches nothing. Dropping the
``/`` from the stored name satisfies both rules at once, so ``org:<id>/<label>-<tool>`` both
resolves and passes policy.

Everything here is PURE — no database, no I/O. Both writers of the shape use it (the importer for
new rows, any migration for old ones), so the two cannot drift apart.

Two things this module deliberately does NOT do:

* It never invents ``spec.capabilities``. No legacy row carries a stored ``inputSchema``, and a
  fabricated empty schema makes a dead tool look callable. Those rows must be re-imported.
* It never mutates its input. A rewrite runs row by row, so an in-place edit would leave
  half-rewritten dictionaries behind after a mid-batch failure.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# ``capability_descriptors.name`` is a String(255). An admin label is capped at 64 by the request
# schema, but the tool name comes from an UNTRUSTED server and is not — so the join must be bounded
# here or the INSERT fails and takes the whole import down with it.
MAX_NAME_LENGTH = 255
_DIGEST_LENGTH = 8


def resolution_slug(name: str) -> str:
    """The slug the runtime matches a capability reference against.

    Mirrors ``_slug`` in the harness runtime's registry client: lowercase, then collapse every run
    of non-alphanumerics to a single ``-``. Two names that differ only by their separators — a
    legacy ``"github-mcp/read"`` and its rewritten ``"github-mcp-read"`` — collapse to the SAME
    slug, which is why the legacy rows still resolve today and why a re-import has to recognise one
    as the older form of the other.
    """
    return _NON_ALNUM.sub("-", name.lower()).strip("-")


def is_mcp_descriptor(descriptor: dict[str, Any]) -> bool:
    """True for a descriptor imported from an MCP server (``spec.type == "mcp"``)."""
    spec = descriptor.get("spec")
    return isinstance(spec, dict) and spec.get("type") == "mcp"


def mcp_capability_name(label: str, tool_name: str) -> str:
    """Build the stored name for one imported tool: ``<label>-<tool_name>``, ``/``-free and bounded.

    The label is admin-supplied and the tool name comes from the server; a ``/`` in either would
    reintroduce the exact defect this shape fixes, so both are flattened. When the join overflows
    the column it is truncated with a short digest of the full name appended, so two long tool names
    sharing a prefix stay distinct instead of silently collapsing into one row.
    """
    joined = f"{_flatten(label)}-{_flatten(tool_name)}".strip("-")
    if len(joined) <= MAX_NAME_LENGTH:
        return joined
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    return f"{joined[: MAX_NAME_LENGTH - _DIGEST_LENGTH - 1]}-{digest}"


def rewrite_mcp_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Move a legacy ``"<label>/<tool>"`` name to ``"<label>-<tool>"``, keeping ``spec.label``.

    Idempotent and total: a non-MCP descriptor, an already-rewritten one, and a name with nothing
    to split are all returned unchanged. Only the name and the recovered label move — the schema,
    the credential and every other spec field are carried across untouched.
    """
    if not is_mcp_descriptor(descriptor):
        return descriptor
    metadata = descriptor.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(name, str) or "/" not in name:
        return descriptor  # already rewritten, or nothing to split — never fabricate a label
    label, _, tool_name = name.partition("/")  # the label is everything before the FIRST separator
    rewritten = copy.deepcopy(descriptor)
    rewritten["metadata"]["name"] = mcp_capability_name(label, tool_name)
    rewritten["spec"]["label"] = label
    return rewritten


def find_name_collisions(names: Iterable[str]) -> set[str]:
    """The names that appear more than once in an organisation's descriptors.

    Nothing in the database catches this: ``name`` carries no unique constraint, and the runtime
    takes the FIRST slug match out of an unordered listing. Two rows sharing a name therefore make
    resolution a coin flip. Labels differing only by a separator — ``acme-prod/read`` and
    ``acme/prod-read`` — collapse to one name, so the case is reachable even though the deployed
    data has none of it today.
    """
    return {name for name, count in Counter(names).items() if count > 1}


def _flatten(part: str) -> str:
    """Strip the one character that cannot appear in a stored name."""
    return part.replace("/", "-")
