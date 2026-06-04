"""knowledge-graph-builder test configuration.

Adds oraclous-core-service/ to sys.path so test modules can import from
app.services.* (which live in oraclous-core-service/app/services/).
"""

from __future__ import annotations

import sys
from pathlib import Path

CORE_SERVICE_DIR = Path(__file__).parent.parent.parent / "oraclous-core-service"

if str(CORE_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_SERVICE_DIR))
