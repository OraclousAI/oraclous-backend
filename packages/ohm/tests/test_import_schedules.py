"""cron.yaml -> ScheduledJob (#408; ADR-034 §6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_.schedules import parse_cron, parse_cron_text

_CRON = """jobs:
  - id: morning_brief
    cron: "0 7 * * *"
    agent: analyst
    entrypoint: harness.jobs.morning_brief:run
    expected_artifact: study/morning-brief.md
  - id: osint_sweep
    cron: "15 * * * *"
    agent: osint-analyst
  - id: broken
    cron: "0 0 * * *"
"""


def test_parses_jobs_skipping_malformed() -> None:
    jobs = parse_cron_text(_CRON)
    assert [j.id for j in jobs] == ["morning_brief", "osint_sweep"]  # 'broken' has no agent
    assert jobs[0].cron == "0 7 * * *"
    assert jobs[0].agent == "analyst"
    assert jobs[0].expected_artifact == "study/morning-brief.md"


def test_non_mapping_fails_closed() -> None:
    with pytest.raises(OHMImportError):
        parse_cron_text("- just\n- a list")


def test_parse_cron_file(tmp_path: Path) -> None:
    p = tmp_path / "cron.yaml"
    p.write_text(_CRON)
    assert len(parse_cron(p)) == 2
