"""Static guardrail: no service is handed a platform MODEL key (#724 / ADR-008 §3.6).

A model call over customer data must run on the ORG's credential, resolved from the
credential-broker. The durable failure mode is not a wrong call at runtime but a **deployment**:
once a service environment holds a model key, any code path can reach it, and the customer's
documents are read by a model they never chose on a key they do not pay for.

This is the third instance of one class. #295 removed a fake-broker default that swapped in platform
resources; #653 removed a silent default that sent a user's SQL ingest at the platform's own
Postgres. Both were fixed as single sites. This linter fixes the class, at the boundary where it
enters.

Two rules:

  BMK001 — a deployment manifest injects a model API key into a service environment, e.g.
           ``KGS_OPENAI_API_KEY: "${OPENROUTER_API_KEY}"`` in compose, or the same name in a Helm
           values env block. A nested default (``"${A:-${B:-}}"``) does not hide it: the name on the
           LEFT is what the container receives.
  BMK002 — a service ``core/config.py`` declares a model-key field (``openai_api_key`` and the
           like), which is the thing such a deployment would populate.

**Infrastructure secrets are not model keys and are exempt**: ``INTERNAL_SERVICE_KEY``,
``AUTH_JWT_SECRET``, every ``OAUTH_*``, and the encryption keys are Oraclous's own identity and
crypto material, not a model reading customer data. A guardrail that fired on those would be
switched off within a week, and then it would protect nothing. Their fail-closed shape is
``check_failclosed_secrets``'s concern, not this one.

**A test-runner env file is not a service environment.** ``deploy/.env.test`` legitimately holds
``TAVILY_API_KEY`` and ``OPENROUTER_API_KEY``: an e2e reads one and pastes it through the
credentials API so it becomes an org credential, which is the pattern this guardrail exists to
protect. The tell is the shape. ``KEY=value`` is an env-file assignment; ``KEY: <value>`` under a
service is an injection into a container.

Run:  uv run python -m tools.lint.check_byom_model_keys [<path> ...]
Defaults to scanning ``deploy`` and ``services``. Exits 1 on any violation, 0 otherwise.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".venv", "venv", "__pycache__", "build", "dist", ".git", "node_modules"}

#: Deployment manifests this guardrail reads (compose, Helm values, K8s manifests).
_MANIFEST_SUFFIXES = (".yml", ".yaml")
#: The service config tail, matching check_failclosed_secrets' policing.
_CONFIG_TAIL = ("core", "config.py")

#: Vendors whose key names may not end in ``_API_KEY`` but are still model keys.
_MODEL_PROVIDER_TOKENS = (
    "OPENAI",
    "ANTHROPIC",
    "OPENROUTER",
    "TAVILY",
    "GEMINI",
    "COHERE",
    "MISTRAL",
    "HUGGINGFACE",
)

#: Oraclous's own material. Platform-owned is CORRECT for these.
_INFRA_EXEMPT = {
    "INTERNAL_SERVICE_KEY",
    "JWT_SECRET",
    "AUTH_JWT_SECRET",
    "ENCRYPTION_KEY",
    "CRED_BROKER_ENCRYPTION_KEY",
    "OAUTH_ENC_KEY",
    "SECRET_KEY",
}

#: ``NAME: value`` — a YAML mapping entry. An env-file ``NAME=value`` deliberately does not match.
_YAML_ENTRY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\S.*)$")


def is_model_key_name(name: str) -> bool:
    """True when ``name`` designates a MODEL provider key rather than infrastructure material."""
    upper = name.upper()
    if upper in _INFRA_EXEMPT or upper.startswith("OAUTH_"):
        return False
    if upper.endswith("_API_KEY"):
        return True
    if any(token in upper for token in _MODEL_PROVIDER_TOKENS):
        return upper.endswith(("_KEY", "_TOKEN"))
    return False


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def check_manifest(source: str, path: str = "<manifest>") -> list[Violation]:
    """BMK001 over a deployment manifest's text."""
    violations: list[Violation] = []
    for lineno, raw in enumerate(source.splitlines(), start=1):
        line = raw.split("#", 1)[0] if raw.lstrip().startswith("#") else raw
        match = _YAML_ENTRY.match(line)
        if match is None:
            continue  # not a mapping entry (an env-file NAME=value lands here, by design)
        name = match.group(1)
        if not is_model_key_name(name):
            continue
        violations.append(
            Violation(
                "BMK001",
                path,
                lineno,
                f"deployment injects model key '{name}' into a service environment; a model call "
                "over customer data must resolve the org's credential from the credential-broker "
                "(#724). Remove the entry.",
            )
        )
    return violations


def check_config_source(source: str, path: str = "<config>") -> list[Violation]:
    """BMK002 over a service ``core/config.py``."""
    violations: list[Violation] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return violations
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets: list[str] = []
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                targets = [node.target.id]
        else:
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        for name in targets:
            if not is_model_key_name(name):
                continue
            violations.append(
                Violation(
                    "BMK002",
                    path,
                    node.lineno,
                    f"service config declares model-key field '{name}'; hold a broker credential "
                    "id instead and resolve the key per call, org-scoped (#724).",
                )
            )
    return violations


def _is_config_file(path: Path) -> bool:
    parts = path.parts
    return len(parts) >= 2 and parts[-2:] == _CONFIG_TAIL


def check_paths(paths: list[str]) -> list[Violation]:
    out: list[Violation] = []
    for raw in paths:
        root = Path(raw)
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for f in candidates:
            if not f.is_file() or any(part in SKIP_DIRS for part in f.parts):
                continue
            if f.suffix in _MANIFEST_SUFFIXES:
                out.extend(check_manifest(f.read_text(encoding="utf-8"), str(f)))
            elif f.suffix == ".py" and _is_config_file(f):
                out.extend(check_config_source(f.read_text(encoding="utf-8"), str(f)))
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    paths = args or ["deploy", "services"]
    violations = check_paths(paths)
    for v in violations:
        print(v)
    if violations:
        print(f"\n{len(violations)} platform-model-key violation(s) found.")
        return 1
    print("check_byom_model_keys: no service is handed a platform model key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
