"""Curated tool-group libraries (#488) — in-repo Python modules exposed as typed tool groups.

A curated library's exported functions are mounted as one tool with one operation per function
(ADR-038 D1). These are trusted, code-reviewed platform code, so :class:`LibraryGroupExecutor` runs
them IN-PROCESS (no subprocess). User-supplied library adoption (the #487 subprocess + RLIMIT + HITL
envelope) is a tracked follow-up, not here.
"""
