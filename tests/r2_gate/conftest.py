"""Conftest for r2_gate acceptance tests.

Adds oraclous-core-service and packages/ohm to sys.path so that
``from app.*`` and ``from schemas.*`` / ``from hashing import ...`` work
without installing the packages into the test environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent  # oraclous-backend/
_CORE_SERVICE = _REPO_ROOT / "oraclous-core-service"
_OHM_PKG = _REPO_ROOT / "packages" / "ohm"
_CAP_REG_SRC = _REPO_ROOT / "services" / "capability-registry-service" / "src"

for _path in (_CORE_SERVICE, _OHM_PKG, _CAP_REG_SRC):
    _s = str(_path)
    if _s not in sys.path:
        sys.path.insert(0, _s)
