"""Repo-root conftest: put the repository root on sys.path so `tools` imports."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
