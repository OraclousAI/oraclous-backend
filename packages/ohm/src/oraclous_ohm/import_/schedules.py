"""Parse a ``cron.yaml`` scheduled-job manifest into member schedules (ADR-034 §6).

A standing team (bitcoin's harness) is triggered by cron jobs, not a pipeline DAG: each job names an
``agent`` and a ``cron`` expr. ``parse_cron`` extracts ``{id, cron, agent, expected_artifact}`` per
job; the assembler (#408) attaches each to its member's ``schedule``. Pure; fail-closed.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from oraclous_ohm.errors import OHMImportError


class ScheduledJob(BaseModel):
    """One cron.yaml job: a cron expression bound to an agent role."""

    model_config = ConfigDict(extra="ignore")

    id: str
    cron: str
    agent: str
    entrypoint: str = ""
    expected_artifact: str = ""


def parse_cron_text(text: str, source: str = "<unknown>") -> list[ScheduledJob]:
    """Parse a cron.yaml document into ``ScheduledJob``s (jobs missing id/cron/agent skipped)."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise OHMImportError(f"{source}: cron.yaml is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise OHMImportError(f"{source}: cron.yaml must be a mapping")
    jobs = doc.get("jobs", [])
    if not isinstance(jobs, list):
        raise OHMImportError(f"{source}: cron.yaml 'jobs' must be a list")
    out: list[ScheduledJob] = []
    for job in jobs:
        if not isinstance(job, dict) or not all(k in job for k in ("id", "cron", "agent")):
            continue
        out.append(
            ScheduledJob(
                id=str(job["id"]),
                cron=str(job["cron"]),
                agent=str(job["agent"]),
                entrypoint=str(job.get("entrypoint", "")),
                expected_artifact=str(job.get("expected_artifact", "")),
            )
        )
    return out


def parse_cron(path: str | Path) -> list[ScheduledJob]:
    """Read and parse a ``cron.yaml`` file (fail-closed on read error)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise OHMImportError(f"cannot read cron file {p}: {exc}") from exc
    return parse_cron_text(text, source=p.name)
