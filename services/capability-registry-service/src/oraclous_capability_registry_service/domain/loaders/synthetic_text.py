"""Synthetic loader that emits NON-JSON to stdout and exits 0 — #487 negative fixture.

Proves the executor maps a clean-exit-but-unparseable output to ``LOADER_BAD_OUTPUT`` rather than
crashing or returning malformed data.
"""

from __future__ import annotations

import sys


def main() -> int:
    sys.stdout.write("this is not json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
