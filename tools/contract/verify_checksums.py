"""CLI: verify (or regenerate) the error-envelope contract fixture checksums.

    uv run python -m tools.contract.verify_checksums           # verify; exit 1 on drift
    uv run python -m tools.contract.verify_checksums --write    # (re)write CHECKSUMS.sha256

The frontend repo mirrors this guard against its copied-with-checksum copy of the
fixture, so a divergence on either side breaks CI (Cross-cutting agreement
protocol §2.6 — a recorded agreement that is not enforced will drift).
"""

from __future__ import annotations

import argparse

from tools.contract.error_envelope import (
    CHECKSUMS_PATH,
    render_checksums,
    verify_checksums,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify error-envelope contract fixture integrity.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="(re)write the checksum manifest instead of verifying",
    )
    args = parser.parse_args(argv)

    if args.write:
        CHECKSUMS_PATH.write_text(render_checksums(), encoding="utf-8")
        print(f"wrote {CHECKSUMS_PATH}")
        return 0

    errors = verify_checksums()
    if errors:
        print("error-envelope contract fixture integrity check FAILED:")
        for err in errors:
            print(f"  - {err}")
        print("\nIf you changed the fixture deliberately, regenerate the manifest with:")
        print("  uv run python -m tools.contract.verify_checksums --write")
        return 1

    print("error-envelope contract fixture integrity OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
