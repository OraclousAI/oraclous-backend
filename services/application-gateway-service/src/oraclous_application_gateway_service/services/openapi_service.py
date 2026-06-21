"""Published OpenAPI contract loader (services layer).

Loads the canonical ``openapi/v1.yaml`` (the R6 public contract, ADR-015) once and caches it. The
route layer serves it at ``/v1/openapi.json`` + ``/docs``. This is a static-asset load — no network,
no database. The spec is resolved from ``OPENAPI_SPEC_PATH`` if set, else by searching upward
for ``openapi/v1.yaml`` (which resolves both in the container image and from a source checkout).
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml


def _resolve_spec_path(override: str) -> Path:
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "openapi" / "v1.yaml"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("openapi/v1.yaml not found; set OPENAPI_SPEC_PATH to its absolute path")


@functools.lru_cache(maxsize=4)
def load_contract(override: str = "") -> tuple[dict, str]:
    """Return ``(spec_dict, raw_yaml)`` for the published contract (cached per path)."""
    path = _resolve_spec_path(override)
    text = path.read_text(encoding="utf-8")
    spec = yaml.safe_load(text)
    version = spec.get("openapi", "") if isinstance(spec, dict) else ""
    if not isinstance(spec, dict) or not str(version).startswith("3."):
        raise ValueError(f"{path} is not a valid OpenAPI 3.x document")
    return spec, text
