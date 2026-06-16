"""Release-coverage guardrail: the v* release publishes EVERY dockerized claimed_done service,
and it scans + SBOMs + signs them (grade-A WP-3 / WP-4).

A tagged release that builds only a subset of the platform cannot deploy it; an image that ships
unscanned/unsigned is not "safe to ship" even when CI is green. This checker ties the release
surface to the no-hollow flags the team already maintains, and to the supply-chain steps, so a
future edit to ``.github/workflows/release.yml`` cannot silently drop a service or strip a gate.

It cross-checks three sets:

  D — services with a ``services/<name>/Dockerfile``.
  C — services with ``claimed_done: true`` in ``tools/lint/service_status.yaml``.
  M — services covered by ``release.yml``'s build matrix.

The required release surface is ``R = D ∩ C`` (dockerized AND claimed_done). The release matrix
must cover all of ``R``. ``release.yml`` derives its matrix from ``service_status.yaml`` (a prior
job emits ``claimed_done && Dockerfile-exists`` as a matrix output), so M is computed the same way
the workflow computes it at runtime — by construction M == R. To prevent that derivation from
silently regressing to a stale subset, the checker requires EITHER:

  * a DERIVED matrix — the workflow references ``service_status.yaml`` and feeds the build matrix
    from a job output via ``fromJSON(...outputs...)`` (the drift-proof form); M is then R, OR
  * a STATIC matrix — an explicit ``matrix: { service: [...] }`` list, which is then checked to
    cover every service in R (a static list is acceptable per the WP-3 spec with this guardrail).

Violations:

  RCV001 — a dockerized claimed_done service is absent from the (static) release matrix.
  RCV002 — release.yml neither derives its matrix from service_status.yaml nor lists one statically.
  RCV003 — a required supply-chain step (trivy / syft / cosign) is missing from release.yml.

Run:  uv run python -m tools.lint.check_release_coverage
Exits non-zero (1) on any violation; 0 otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVICES_DIR = _REPO_ROOT / "services"
_STATUS_PATH = _REPO_ROOT / "tools" / "lint" / "service_status.yaml"
_RELEASE_YML = _REPO_ROOT / ".github" / "workflows" / "release.yml"

# Required supply-chain steps (WP-4): each is matched case-insensitively as a substring of the
# release workflow text. Trivy = scan, Syft = SBOM, cosign = sign + attest.
_REQUIRED_SUPPLYCHAIN = ("trivy", "syft", "cosign")


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule} {self.message}"


def dockerized_services(services_dir: Path = _SERVICES_DIR) -> set[str]:
    """Set D — service dirs that contain a Dockerfile."""
    if not services_dir.is_dir():
        return set()
    return {
        child.name
        for child in services_dir.iterdir()
        if child.is_dir() and (child / "Dockerfile").is_file()
    }


def claimed_done_services(status_path: Path = _STATUS_PATH) -> set[str]:
    """Set C — services flagged claimed_done: true in service_status.yaml."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - yaml is always present in this repo's venv
        return set()
    data = yaml.safe_load(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    services = (data or {}).get("services", {}) or {}
    return {name for name, meta in services.items() if (meta or {}).get("claimed_done")}


def _static_matrix_services(text: str) -> set[str] | None:
    """If release.yml declares a STATIC ``matrix: service: [...]`` list, return that set.

    Returns ``None`` when no static service list is present (i.e. the matrix is derived at runtime).
    Handles both flow ``[a, b]`` and block ``- a`` YAML list styles for a ``service:`` matrix key.
    """
    # Flow style:  service: [auth-service, credential-broker-service, ...]
    flow = re.search(r"^\s*service:\s*\[([^\]]*)\]", text, re.MULTILINE)
    if flow:
        items = [s.strip().strip("'\"") for s in flow.group(1).split(",")]
        return {s for s in items if s}
    # Block style:
    #   service:
    #     - auth-service
    #     - credential-broker-service
    block = re.search(r"^(\s*)service:\s*\n((?:\1\s+-\s*.+\n?)+)", text, re.MULTILINE)
    if block:
        items = re.findall(r"-\s*([^\s#]+)", block.group(2))
        return {s.strip().strip("'\"") for s in items if s.strip()}
    return None


def _matrix_is_derived(text: str) -> bool:
    """True when the build matrix is fed from a job output that reads service_status.yaml.

    The drift-proof form: the workflow references ``service_status.yaml`` AND a matrix
    ``service:`` value built via ``fromJSON(... .outputs. ...)``.
    """
    references_status = "service_status.yaml" in text
    derived_matrix = bool(
        re.search(
            r"service:\s*\$\{\{\s*fromJSON\([^)]*outputs[^)]*\)\s*\}\}",
            text,
        )
    )
    return references_status and derived_matrix


def check() -> list[Violation]:
    violations: list[Violation] = []

    if not _RELEASE_YML.is_file():
        return [Violation("RCV002", f"{_RELEASE_YML} does not exist")]
    text = _RELEASE_YML.read_text(encoding="utf-8")

    required = dockerized_services() & claimed_done_services()  # R = D ∩ C

    derived = _matrix_is_derived(text)
    static = _static_matrix_services(text)

    if derived:
        # M == R by construction (the workflow computes the same intersection at runtime).
        pass
    elif static is not None:
        missing = sorted(required - static)
        for svc in missing:
            violations.append(
                Violation(
                    "RCV001",
                    f"dockerized claimed_done service '{svc}' is missing from the release build "
                    "matrix in .github/workflows/release.yml.",
                )
            )
    else:
        violations.append(
            Violation(
                "RCV002",
                ".github/workflows/release.yml neither derives its build matrix from "
                "service_status.yaml (fromJSON of a job output) nor declares a static "
                "'matrix: service: [...]' list — cannot verify release coverage.",
            )
        )

    low = text.lower()
    for step in _REQUIRED_SUPPLYCHAIN:
        if step not in low:
            violations.append(
                Violation(
                    "RCV003",
                    f"required supply-chain step '{step}' is absent from "
                    ".github/workflows/release.yml (scan/SBOM/sign must not be stripped).",
                )
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    violations = check()
    for v in violations:
        print(v)
    if violations:
        print(f"\n{len(violations)} release-coverage violation(s) found.")
        return 1
    print(
        "release-coverage OK: all dockerized claimed_done services covered; "
        "trivy/syft/cosign present."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
