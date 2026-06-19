"""Adoption-First import (ADR-034): read an existing agent setup and emit a runnable OHM v1.1 Team
Harness. Pure (filesystem-in, OHM-out). Slice 1 (#405) is the ``.claude/agents/*.md`` parser; the
frontmatter->member mapping, skill inlining, charter/orchestrator adapters, DAG-from-source, and the
dry-run land in #406-#409.
"""
