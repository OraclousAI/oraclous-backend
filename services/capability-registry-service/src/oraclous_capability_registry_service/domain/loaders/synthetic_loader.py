"""Synthetic curated loader (#487) — a dependency-free sample that emits JSON records to stdout.

Run as a subprocess by :class:`ScriptIngestionConnector` to prove the script-as-scheduled-ingestion
path end-to-end: an unmodified loader → registry subprocess → captured output → the org-store
Execution row. Deterministic, no network, no external API, so the deployed e2e is key-free and
self-contained. Reads ``--count N`` from argv and prints ``{"records": [...], ...}``.
"""

from __future__ import annotations

import argparse
import json
import sys

_MAX_COUNT = 1000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="synthetic ingestion loader")
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args(argv)
    count = max(0, min(args.count, _MAX_COUNT))
    records = [{"id": i, "title": f"synthetic-row-{i}"} for i in range(1, count + 1)]
    json.dump({"records": records, "source": "synthetic", "count": len(records)}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
