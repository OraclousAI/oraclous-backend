"""Synthetic loader that FAILS (exit non-zero, writes to stderr) — #487 negative fixture.

Proves the executor maps a non-zero exit to a coarse ``LOADER_FAILED`` and **never echoes stderr**
(an arbitrary loader's stderr can carry secrets/paths). The stderr line below is a deliberate
canary the e2e/unit asserts does NOT appear in the structured failure.
"""

from __future__ import annotations

import sys


def main() -> int:
    print("LOADER-INTERNAL-CANARY: /etc/passwd secret-trace", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
