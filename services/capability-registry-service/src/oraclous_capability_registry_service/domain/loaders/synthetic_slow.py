"""Synthetic loader that HANGS — #487 negative fixture for the timeout + process-group kill.

Sleeps well past any test's inner subprocess cap so the executor must time it out and SIGKILL the
whole process group (``start_new_session`` + ``os.killpg``). Used with a small overridden
``subprocess_timeout_s`` so the unit test still runs fast.
"""

from __future__ import annotations

import sys
import time


def main() -> int:
    time.sleep(120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
