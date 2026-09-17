"""Curated per-member error tokens (domain layer) — pure, no I/O.

``EngineTeamRun.member_error_codes`` maps a member's role to one of these tokens when its harness
run FAILED with an allow-listed ``error_type`` (#1108 ruling 2c). The token is curated: it never
carries provider text, so it is safe to relay to a caller and to branch on.

The tokens MIRROR the harness runtime's own constants (``domain/loop/tool_use.py``). The engine
cannot import them — the harness runtime is a sibling Layer-3 service, and a cross-service import
would violate the layering (ADR-001, CLAUDE.md §3.1) — so each literal is pinned here, once, and
every engine-side comparison reads it from this module rather than re-typing the string.
"""

from __future__ import annotations

#: A member whose model provider refused the bound key (401/403). Mirrors
#: ``LLM_CREDENTIAL_REJECTED`` in the harness runtime's ``domain/loop/tool_use.py``.
LLM_CREDENTIAL_REJECTED = "llm_credential_rejected"
