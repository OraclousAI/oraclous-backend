"""
[tests] workflow retirement — unit (structural) — ORAA-78

Story: ORAA-78 / ORA-77
Architecture refs:
  - ADR-005 (retire workflow_service / pipeline_generator):
      https://oraclous.atlassian.net/wiki/spaces/OP/pages/753772
  - Test Strategy: https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

These tests assert the ABSENCE of workflow_service.py and pipeline_generator.py
and all their import sites.  Every test is intentionally red until the implementer
completes the deletion described in ORAA-78.

Behaviours covered:
  W01  app/services/workflow_service.py file is deleted
  W02  app/services/pipeline_generator.py file is deleted
  W03  app/api/v1/endpoints/workflow_routes.py file is deleted (importer removed)
  W04  app.api.v1.router no longer references workflow_routes
  W05  no Python source file in app/ imports from app.services.workflow_service
  W06  no Python source file in app/ contains any reference to pipeline_generator or PipelineGenerator

NOTE on W01/W02 approach: file-existence checks are used (not import-error assertions)
because the workflow modules import optional dependencies (langgraph, etc.) that may
not be installed in the test environment, causing ImportError before the file is even
deleted.  A file-existence check is unambiguous: it is red when the file exists and
green only when the file is gone.
"""

from __future__ import annotations

import re  # used by _WORKFLOW_SERVICE_IMPORT_RE (W05)
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Repo path anchors used by filesystem-scan tests
# ---------------------------------------------------------------------------

_APP_DIR = Path(__file__).parent.parent.parent  # oraclous-core-service/app/
_THIS_FILE = Path(__file__)


# ---------------------------------------------------------------------------
# W01  workflow_service.py file is deleted
#
# Currently FAILS: app/services/workflow_service.py still exists.
# Passes after the implementer deletes workflow_service.py.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_workflow_service_file_deleted():
    """
    app/services/workflow_service.py must not exist after ADR-005 retirement.
    WorkflowService held LangGraph placeholder stubs with no production callers;
    it is removed entirely, not migrated.
    """
    workflow_service_path = _APP_DIR / "services" / "workflow_service.py"
    assert not workflow_service_path.exists(), (
        f"workflow_service.py still exists at {workflow_service_path} — "
        "delete it as part of ORAA-78 (archive DB rows first)"
    )


# ---------------------------------------------------------------------------
# W02  pipeline_generator.py file is deleted
#
# Currently FAILS: app/services/pipeline_generator.py still exists.
# Passes after the implementer deletes pipeline_generator.py.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_generator_file_deleted():
    """
    app/services/pipeline_generator.py must not exist after ADR-005 retirement.
    PipelineGenerator contained placeholder stubs and had no production caller;
    it is removed entirely.
    """
    pipeline_generator_path = _APP_DIR / "services" / "pipeline_generator.py"
    assert not pipeline_generator_path.exists(), (
        f"pipeline_generator.py still exists at {pipeline_generator_path} — "
        "delete it as part of ORAA-78"
    )


# ---------------------------------------------------------------------------
# W03  workflow_routes.py file is deleted (it imports from the deleted modules)
#
# Currently FAILS: app/api/v1/endpoints/workflow_routes.py still exists.
# Passes after the implementer deletes the routes module.
#
# NOTE: import-error approach not used here because importing workflow_routes
# triggers Pydantic Settings startup validation (INTERNAL_SERVICE_KEY env var
# required), which raises ValidationError rather than ImportError.
# A direct file-existence check is reliable and unambiguous.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_workflow_routes_file_deleted():
    """
    app/api/v1/endpoints/workflow_routes.py must not exist after ADR-005 retirement.
    workflow_routes.py is directly coupled to WorkflowService (deleted); the
    entire routes module is removed because no production endpoint replaces it.
    """
    workflow_routes_path = _APP_DIR / "api" / "v1" / "endpoints" / "workflow_routes.py"
    assert not workflow_routes_path.exists(), (
        f"workflow_routes.py still exists at {workflow_routes_path} — "
        "delete it as part of ORAA-78 after removing the router.py mounting entry"
    )


# ---------------------------------------------------------------------------
# W04  router.py no longer mounts the workflow router
#
# Currently FAILS: app/api/v1/router.py imports workflow_routes and calls
# include_router with it.
# Passes after the implementer removes that import and the include_router call.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_router_does_not_import_workflow_routes():
    """
    app/api/v1/router.py must not contain any reference to workflow_routes.
    The router is the public mounting point; leaving a dead import there would
    cause an ImportError at app startup after the routes module is deleted.
    """
    router_path = _APP_DIR / "api" / "v1" / "router.py"
    assert router_path.exists(), f"router.py not found at {router_path}"
    content = router_path.read_text()
    assert "workflow_routes" not in content, (
        "router.py still references workflow_routes — remove the import and "
        "include_router call as part of ORAA-78"
    )


# ---------------------------------------------------------------------------
# W05  no Python source in app/ imports workflow_service
#
# Currently FAILS: workflow_routes.py has
#   "from app.services.workflow_service import WorkflowService"
# Passes after the implementer removes workflow_routes.py and any other callers.
# ---------------------------------------------------------------------------

_WORKFLOW_SERVICE_IMPORT_RE = re.compile(
    r"(from\s+app\.services\.workflow_service\b"
    r"|import\s+app\.services\.workflow_service\b)"
)


@pytest.mark.unit
def test_no_python_source_imports_workflow_service():
    """
    Zero Python files in app/ may contain an import of app.services.workflow_service.
    Grep-clean assertion: the implementer must remove every import site before
    this test passes.  The migrations/ subtree is exempt.
    """
    culprits = [
        str(f.relative_to(_APP_DIR))
        for f in _APP_DIR.rglob("*.py")
        if "migrations" not in f.parts
        and "tests"
        not in f.parts  # test files may reference deleted names in docstrings
        and _WORKFLOW_SERVICE_IMPORT_RE.search(f.read_text())
    ]
    assert not culprits, (
        f"{len(culprits)} file(s) still import workflow_service:\n"
        + "\n".join(f"  {c}" for c in culprits)
    )


# ---------------------------------------------------------------------------
# W06  no Python source in app/ references pipeline_generator
#
# Currently FAILS: pipeline_generator.py itself exists and contains
# "PipelineGenerator"; workflow_service.py also references PipelineGenerator
# in a comment.  Both files must be deleted by the implementer.
# Passes after the implementer deletes pipeline_generator.py and workflow_service.py.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_python_source_references_pipeline_generator():
    """
    Zero Python files in app/ may contain any reference to 'pipeline_generator'
    or 'PipelineGenerator' after ADR-005 retirement.  The grep-clean check covers
    the module file itself, import statements, and inline comments — all must be gone.
    The migrations/ subtree is exempt.
    """
    culprits = [
        str(f.relative_to(_APP_DIR))
        for f in _APP_DIR.rglob("*.py")
        if "migrations" not in f.parts
        and "tests"
        not in f.parts  # test files may reference deleted names in docstrings
        and (
            "pipeline_generator" in f.read_text()
            or "PipelineGenerator" in f.read_text()
        )
    ]
    assert not culprits, (
        f"{len(culprits)} file(s) still reference pipeline_generator:\n"
        + "\n".join(f"  {c}" for c in culprits)
    )
