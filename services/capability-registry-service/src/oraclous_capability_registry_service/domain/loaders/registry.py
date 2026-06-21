"""Curated loader registry (#487) — the ONLY loaders a script-ingestion request may run.

A request's ``loader_id`` selects a :class:`LoaderSpec` here; the request body NEVER supplies a free
argv or entrypoint, so no arbitrary code path exists (ADR-038 D5; user-supplied loaders + HITL are a
follow-up). Each spec resolves to a packaged module run with the venv interpreter
(``sys.executable`` + the module's in-image path via :func:`importlib.util.find_spec`), so it
and runs in-container with no ``PYTHONPATH``/cwd assumptions.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass

_PKG = "oraclous_capability_registry_service.domain.loaders"


@dataclass(frozen=True)
class LoaderSpec:
    """A curated loader: its id, the importable module to run, and whether it needs a BYOM key."""

    loader_id: str
    module: str
    requires_api_key: bool = False

    def module_path(self) -> str:
        """The loader module's absolute on-disk path (resolved in the running image)."""
        spec = importlib.util.find_spec(self.module)
        if spec is None or spec.origin is None:
            raise LookupError(f"loader module not importable: {self.module}")
        return spec.origin

    def command(self, argv: list[str]) -> list[str]:
        """The full argv to exec: ``[venv-python, <module path>, *loader-args]`` (never a shell)."""
        return [sys.executable, self.module_path(), *argv]


_LOADERS: dict[str, LoaderSpec] = {
    s.loader_id: s
    for s in (
        LoaderSpec("synthetic", f"{_PKG}.synthetic_loader"),
        LoaderSpec("synthetic-fail", f"{_PKG}.synthetic_fail"),
        LoaderSpec("synthetic-slow", f"{_PKG}.synthetic_slow"),
        LoaderSpec("synthetic-text", f"{_PKG}.synthetic_text"),
    )
}


def get_loader(loader_id: str) -> LoaderSpec | None:
    """The curated loader for ``loader_id``, or ``None`` if it is not a known curated loader."""
    return _LOADERS.get(loader_id)


def available_loaders() -> list[str]:
    """The curated loader ids (stable order) — for diagnostics / the descriptor."""
    return sorted(_LOADERS)
