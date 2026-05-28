"""Pytest configuration for the oraclous-substrate package test suite.

Registers the ``rebac`` and ``audit`` markers used by the substrate seam
tests. These markers are named by the Structured Threat Catalogue (T1-M2 uses
``rebac``; T7-M1 uses ``audit``) but are not yet part of the canonical Test
Strategy marker list registered in the repo-root ``pytest.ini``. They are
declared here — local to this package's tests — so the mandated tests run
under ``--strict-markers`` without modifying the shared pytest configuration.

Flagged on ORA-15 for solution-architect / docs-writer: decide whether
``rebac`` and ``audit`` join the canonical Test Strategy taxonomy and move to
``pytest.ini``.
"""

from __future__ import annotations


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "rebac: ReBAC access-decision tests (Structured Threat Catalogue T1-M2)",
    )
    config.addinivalue_line(
        "markers",
        "audit: provenance / audit-event tests (Structured Threat Catalogue T7-M1)",
    )
