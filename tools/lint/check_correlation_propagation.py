"""Static guardrail: end-to-end correlation-id propagation (WP-6).

Two recurrence classes this guards against:

  CORR001 — the gateway proxy stops forwarding the correlation id. The gateway mints a
            server-authoritative ``X-Request-Id`` at the edge but historically dropped it at the
            proxy boundary (WP-6 "current state"). This asserts
            ``services/proxy_service.py``'s ``forward_request_headers`` still appends an
            ``x-request-id`` header — so the id survives gateway → upstream.

  CORR002 — a service stops wiring the shared telemetry logging/correlation config. WP-6 puts the
            JSON structured-logging config + correlation-id middleware in ``oraclous_telemetry`` and
            requires every service to wire it at app startup ("a service that logs without the
            structured config" is a contract failure). This asserts every service's ``app/factory``
            installs the shared telemetry — either via ``install_telemetry(...)`` (the uniform
            one-liner) or, for the gateway which owns its own request-id middleware, via
            ``configure_structured_logging(...)``.

Best-effort static analysis (substring/AST checks over the source) — the definitive behavioural
proof is the contract test in ``tests/contract/test_correlation_propagation.py``. This lint is the
cheap always-green merge gate that catches the obvious regression shapes.

Run:  uv run python -m tools.lint.check_correlation_propagation [<repo_root>]
Exits non-zero (1) if any violation is found; 0 otherwise.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Each service's app/factory.py (the only sanctioned wiring point) keyed by package dir.
_SERVICES: tuple[str, ...] = (
    "auth-service",
    "credential-broker-service",
    "knowledge-graph-service",
    "knowledge-retriever-service",
    "capability-registry-service",
    "harness-runtime-service",
    "execution-engine-service",
    "application-gateway-service",
)


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.rule} {self.message}"


def _factory_path(repo_root: Path, service: str) -> Path:
    pkg = "oraclous_" + service.replace("-", "_")
    return repo_root / "services" / service / "src" / pkg / "app" / "factory.py"


def _check_proxy_forwards_request_id(repo_root: Path) -> list[Violation]:
    proxy = (
        repo_root
        / "services"
        / "application-gateway-service"
        / "src"
        / "oraclous_application_gateway_service"
        / "services"
        / "proxy_service.py"
    )
    if not proxy.is_file():
        return [Violation("CORR001", str(proxy), "gateway proxy_service.py not found")]
    src = proxy.read_text(encoding="utf-8")
    if "def forward_request_headers" not in src:
        return [
            Violation(
                "CORR001", str(proxy), "forward_request_headers not found in proxy_service.py"
            )
        ]
    # The forwarded header is appended as a (b"x-request-id", ...) tuple.
    if 'b"x-request-id"' not in src and "b'x-request-id'" not in src:
        return [
            Violation(
                "CORR001",
                str(proxy),
                "forward_request_headers does not forward an x-request-id header upstream "
                "(WP-6: the correlation id must survive gateway → upstream)",
            )
        ]
    return []


def _check_service_wires_telemetry(repo_root: Path, service: str) -> list[Violation]:
    factory = _factory_path(repo_root, service)
    if not factory.is_file():
        return [Violation("CORR002", str(factory), f"{service} app/factory.py not found")]
    src = factory.read_text(encoding="utf-8")
    # Uniform wiring is install_telemetry(app); the gateway owns its own request-id middleware and
    # installs only the structured-logging config (configure_structured_logging).
    if "install_telemetry(" in src or "configure_structured_logging(" in src:
        return []
    return [
        Violation(
            "CORR002",
            str(factory),
            f"{service} app/factory does not wire the shared telemetry "
            "(call install_telemetry(app) — or configure_structured_logging() for the gateway). "
            "WP-6: every service logs through the structured config.",
        )
    ]


def check(repo_root: Path | None = None) -> list[Violation]:
    root = repo_root or _REPO_ROOT
    out: list[Violation] = []
    out.extend(_check_proxy_forwards_request_id(root))
    for service in _SERVICES:
        out.extend(_check_service_wires_telemetry(root, service))
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    repo_root = Path(args[0]) if args else _REPO_ROOT
    violations = check(repo_root)
    for v in violations:
        print(v)
    if violations:
        print(f"\n{len(violations)} correlation-propagation violation(s) found.")
        return 1
    print("correlation-propagation guardrail: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
