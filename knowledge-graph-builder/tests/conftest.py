"""knowledge-graph-builder test configuration.

Adds knowledge-graph-builder/ to sys.path so test modules can import from
app.services.* without a PYTHONPATH env var.
"""

from __future__ import annotations

import sys
from pathlib import Path

KGB_DIR = Path(__file__).parent.parent  # knowledge-graph-builder/

if str(KGB_DIR) not in sys.path:
    sys.path.insert(0, str(KGB_DIR))
