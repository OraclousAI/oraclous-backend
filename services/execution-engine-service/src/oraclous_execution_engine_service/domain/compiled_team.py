"""The compile-run predicate (#1169, domain layer).

A build run that succeeds on a retry is never saved as a team when the console tab that started it
is gone, so the engine saves the compiled team at settle time. It must do so ONLY for a compile run,
never for an ordinary user team or the op-drafter / app-form-drafter one-member teams the engine
also runs. This module is pure: a plain manifest dict in, a bool out, no I/O.
"""

from __future__ import annotations

from typing import Any

_COMPILER_TEAM_NAME = "harness-compiler"
_REVIEWER_ROLE = "reviewer"
_REVIEWER_MANIFEST_REF = "org:compiler/reviewer@1"


def is_compile_run(manifest: dict[str, Any]) -> bool:
    """True iff this is the compiler team: its name AND its compiler reviewer member.

    Both are required: the name alone is a user's to choose, and a reviewer member alone could
    appear in any team. A malformed or partial manifest is ``False``, never an error.
    """
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("name") != _COMPILER_TEAM_NAME:
        return False
    members = manifest.get("members")
    if not isinstance(members, list):
        return False
    return any(
        isinstance(member, dict)
        and member.get("role") == _REVIEWER_ROLE
        and member.get("manifest_ref") == _REVIEWER_MANIFEST_REF
        for member in members
    )
