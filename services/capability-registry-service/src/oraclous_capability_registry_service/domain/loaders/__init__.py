"""Curated ingestion loaders (#487) — the in-repo loader modules a script-ingestion tool may run.

These ship inside the package (so they land in the runtime image and run in-container) and are the
ONLY loaders a script-ingestion request can select, via :mod:`registry`. A request never supplies a
free argv/entrypoint — user-supplied loader adoption (HITL + content-pinning, ADR-038 D4) is a
tracked follow-up, so no arbitrary code runs here.
"""
