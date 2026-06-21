"""Stage 0 — repository bootstrap (services layer — key-free, no DB).

Faithful lift-and-reshape of legacy `develop@84152635 code_parser_service.bootstrap_repository`
(Stage 0): resolve a source tree (one of two ways), walk it, and parse dependency manifests into
``Dependency`` rows. The two source channels:

  * **uploaded bytes** (a ``.zip`` of sources, or a single source file) — the existing R3.5 path,
    walked in-memory with no filesystem write;
  * **git_url** (optional) — a shallow ``git clone`` into a temp dir, then the same walk. This is an
    egress/SSRF surface, so it runs ONLY when the operator opts in via ``KGS_CODE_CLONE_ENABLED``.
    Host validation (the HRS/CRS ``domain/egress.py`` pattern) is a tracked follow-up — #307 owns
    egress; until it lands, clone is gated behind the flag in a trusted operator context, never
    reachable from an arbitrary uploaded payload.

The walk skips hidden dirs / ``node_modules`` / ``__pycache__`` / ``venv`` (the legacy skip set —
deliberately NOT ``dist``/``build``, which legitimately ship source in some projects), caps file
count + per-file bytes, and yields ``(rel_path, raw_bytes)`` for every supported source file.
Manifests (``requirements.txt`` / ``package.json`` / ``go.mod`` / ``pom.xml``) are parsed into
``Dependency`` nodes regardless of source channel.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess  # noqa: S404 — git clone is the bootstrap channel, flag-gated + arg-list (no shell)
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from oraclous_knowledge_graph_service.services.code.parser import language_for

logger = logging.getLogger(__name__)

_MAX_FILES = 5000
_MAX_FILE_BYTES = 2_000_000
# The legacy skip set (code_parser_service): vendored/build-cache dirs only. NOT dist/build —
# projects legitimately ship source under those, so skipping them under-ingests (review NIT #8).
_SKIP_DIR_PARTS = {".git", "node_modules", "__pycache__", "venv", ".venv"}

# Dependency manifests parsed into :Dependency nodes (legacy MANIFEST_FILES, code-graph subset).
_MANIFEST_FILES = {"requirements.txt", "package.json", "go.mod", "pom.xml"}

_REQ_RE = re.compile(r"^([A-Za-z0-9_\-.]+)\s*([>=<!~^].*)?$")
_GOMOD_RE = re.compile(r"^\s+(\S+)\s+(\S+)")


@dataclass(frozen=True)
class Dependency:
    """A third-party dependency declared by a manifest (Stage 0 -> :Dependency node)."""

    name: str
    version_constraint: str = ""
    dep_type: str = "runtime"


class CodeCloneDisabledError(Exception):
    """A ``git_url`` was supplied but ``KGS_CODE_CLONE_ENABLED`` is off (egress gate, #305/#307)."""


def _skip_path(rel_path: str) -> bool:
    parts = rel_path.split("/")
    return any(p in _SKIP_DIR_PARTS or p.startswith(".") for p in parts[:-1])


def iter_zip_sources(zip_bytes: bytes) -> Iterator[tuple[str, bytes]]:
    """Yield ``(path, raw)`` for every supported source file in an uploaded zip (in-memory walk)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.file_size > _MAX_FILE_BYTES:
                continue
            if _skip_path(info.filename):
                continue
            if language_for(info.filename) is None:
                continue
            yield info.filename, zf.read(info)


def iter_zip_manifests(zip_bytes: bytes) -> Iterator[tuple[str, bytes]]:
    """Yield ``(filename, raw)`` for every dependency manifest in an uploaded zip."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.file_size > _MAX_FILE_BYTES:
                continue
            if _skip_path(info.filename):
                continue
            name = info.filename.split("/")[-1]
            if name in _MANIFEST_FILES:
                yield name, zf.read(info)


def _walk_dir_sources(root: str) -> Iterator[tuple[str, bytes]]:
    """Yield ``(rel_path, raw)`` for supported source files under a directory (cloned-repo walk)."""
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_PARTS and not d.startswith(".")]
        for fname in files:
            abs_path = os.path.join(cur, fname)
            rel_path = os.path.relpath(abs_path, root)
            if language_for(fname) is None:
                continue
            try:
                if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
                    continue
                yield rel_path, Path(abs_path).read_bytes()
            except OSError as exc:
                logger.warning("could not read %s: %s", abs_path, exc)


def _walk_dir_manifests(root: str) -> Iterator[tuple[str, bytes]]:
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_PARTS and not d.startswith(".")]
        for fname in files:
            if fname in _MANIFEST_FILES:
                abs_path = os.path.join(cur, fname)
                try:
                    yield fname, Path(abs_path).read_bytes()
                except OSError as exc:
                    logger.warning("could not read manifest %s: %s", abs_path, exc)


def parse_manifest(filename: str, raw: bytes) -> list[Dependency]:
    """Parse one manifest's bytes into ``Dependency`` rows (best-effort, never raises)."""
    deps: list[Dependency] = []
    try:
        text = raw.decode("utf-8", errors="ignore")
        if filename == "requirements.txt":
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = _REQ_RE.match(line)
                if m:
                    deps.append(Dependency(name=m.group(1), version_constraint=m.group(2) or ""))
        elif filename == "package.json":
            data = json.loads(text)
            for section, dep_type in (
                ("dependencies", "runtime"),
                ("devDependencies", "dev"),
                ("optionalDependencies", "optional"),
            ):
                for name, ver in (data.get(section) or {}).items():
                    deps.append(
                        Dependency(name=name, version_constraint=str(ver), dep_type=dep_type)
                    )
        elif filename == "go.mod":
            for line in text.splitlines():
                m = _GOMOD_RE.match(line)
                if m:
                    deps.append(Dependency(name=m.group(1), version_constraint=m.group(2)))
        elif filename == "pom.xml":
            deps.extend(_parse_pom(text))
    except Exception as exc:  # noqa: BLE001 — a malformed manifest is logged, never fatal
        logger.warning("failed to parse manifest %s: %s", filename, exc)
    return deps


def _parse_pom(text: str) -> list[Dependency]:
    """Extract Maven <dependency> coordinates, namespace-agnostic (strip the POM namespace)."""
    deps: list[Dependency] = []
    root = ET.fromstring(text)  # noqa: S314 — manifest from a trusted operator-supplied repo

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    for dep in root.iter():
        if _local(dep.tag) != "dependency":
            continue
        fields = {_local(child.tag): (child.text or "").strip() for child in dep}
        group = fields.get("groupId", "")
        artifact = fields.get("artifactId", "")
        name = f"{group}:{artifact}".strip(":")
        if name:
            deps.append(
                Dependency(
                    name=name,
                    version_constraint=fields.get("version", ""),
                    dep_type=fields.get("scope", "runtime") or "runtime",
                )
            )
    return deps


def _dedup_deps(deps: list[Dependency]) -> list[Dependency]:
    seen: dict[str, Dependency] = {}
    for d in deps:
        if d.name not in seen:
            seen[d.name] = d
    return list(seen.values())


def clone_repository(git_url: str, branch: str, *, clone_enabled: bool) -> str:
    """Shallow-clone ``git_url`` into a temp dir; return the path. Caller owns cleanup.

    Egress gate (#305): clone runs ONLY when ``clone_enabled`` (``KGS_CODE_CLONE_ENABLED``). The
    full SSRF host check is a tracked follow-up (#307 owns egress); until then this is a flag-gated
    trusted-operator path, never reachable from an arbitrary uploaded payload.
    """
    if not clone_enabled:
        raise CodeCloneDisabledError(
            "git_url ingestion is disabled (set KGS_CODE_CLONE_ENABLED to opt in; "
            "host egress validation is tracked as #307)"
        )
    git = shutil.which("git")
    if git is None:
        raise CodeCloneDisabledError("git executable not found on PATH")
    tmp = tempfile.mkdtemp(prefix="kgs_code_")
    args = [git, "clone", "--depth=1"]
    if branch:
        args += ["--branch", branch]
    args += ["--", git_url, tmp]
    logger.info("cloning %s@%s", git_url, branch or "<default>")
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=300)  # noqa: S603 — arg-list, no shell
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp


def bootstrap(
    *,
    document: str,
    data: bytes,
    git_url: str | None = None,
    branch: str = "",
    clone_enabled: bool = False,
) -> tuple[list[tuple[str, bytes]], list[Dependency]]:
    """Resolve the source tree and return ``(sources, dependencies)``.

    ``sources`` is ``[(rel_path, raw_bytes), ...]`` for every supported source file (capped at
    ``_MAX_FILES``); ``dependencies`` is the de-duplicated manifest-derived dependency list. A
    ``git_url`` (flag-gated) clones+walks a real repo; otherwise the uploaded ``data`` (zip or one
    file) is walked in-memory — the existing R3.5 channel.
    """
    sources: list[tuple[str, bytes]]
    manifests: list[tuple[str, bytes]]
    if git_url:
        repo_path = clone_repository(git_url, branch, clone_enabled=clone_enabled)
        try:
            sources = list(_walk_dir_sources(repo_path))
            manifests = list(_walk_dir_manifests(repo_path))
        finally:
            shutil.rmtree(repo_path, ignore_errors=True)
    elif document.lower().endswith(".zip") or data[:2] == b"PK":
        sources = list(iter_zip_sources(data))
        manifests = list(iter_zip_manifests(data))
    elif language_for(document):
        sources = [(document, data)]
        manifests = []
    else:
        raise CodeIngestionSourceError(f"unsupported code source: {document!r}")

    if len(sources) > _MAX_FILES:
        sources = sources[:_MAX_FILES]

    deps: list[Dependency] = []
    for name, raw in manifests:
        deps.extend(parse_manifest(name, raw))
    return sources, _dedup_deps(deps)


class CodeIngestionSourceError(Exception):
    """The supplied code source could not be resolved (not a zip / unsupported file)."""
