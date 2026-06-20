"""#466: ``provision_app_role`` serializes concurrent migrate containers with a session advisory
lock around the role-provisioning DDL, so the shared cluster-level ``oraclous_app`` CREATE ROLE +
GRANTs don't race the catalog (``tuple concurrently updated``)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from oraclous_substrate.access_async import _PROVISION_ROLE_LOCK_KEY, provision_app_role

pytestmark = [pytest.mark.unit]

_TEST_PW = "pw"  # noqa: S105 — a dummy role password for the DDL call, not a real secret


def _recording_conn(calls: list[tuple[str, object]], fail_on: str | None = None) -> MagicMock:
    cur = MagicMock()

    def _execute(sql: object, params: object = None) -> None:
        calls.append((str(sql), params))
        if fail_on is not None and fail_on in str(sql):
            raise RuntimeError("ddl boom")

    cur.execute.side_effect = _execute
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cm
    return conn


def test_provisioning_is_bracketed_by_a_session_advisory_lock() -> None:
    calls: list[tuple[str, object]] = []
    provision_app_role(
        _recording_conn(calls), role="oraclous_app", password=_TEST_PW, tables=("t",)
    )
    sqls = [c[0] for c in calls]

    # the lock is taken FIRST and released LAST, both on the shared key
    assert "pg_advisory_lock" in sqls[0]
    assert calls[0][1] == (_PROVISION_ROLE_LOCK_KEY,)
    assert "pg_advisory_unlock" in sqls[-1]
    assert calls[-1][1] == (_PROVISION_ROLE_LOCK_KEY,)

    # the DDL (CREATE ROLE + GRANTs) runs strictly BETWEEN the lock and unlock
    middle = sqls[1:-1]
    assert any("CREATE ROLE" in s for s in middle)
    assert any("GRANT" in s for s in middle)
    assert all("pg_advisory" not in s for s in middle)


def test_unlock_runs_even_when_a_ddl_statement_fails() -> None:
    """The unlock is in a ``finally`` — a failing DDL still releases the lock, so one crashing
    migrate container never wedges the others."""
    calls: list[tuple[str, object]] = []
    with pytest.raises(RuntimeError):
        provision_app_role(
            _recording_conn(calls, fail_on="CREATE ROLE"),
            role="oraclous_app",
            password=_TEST_PW,
            tables=("t",),
        )
    assert "pg_advisory_unlock" in calls[-1][0]
    assert calls[-1][1] == (_PROVISION_ROLE_LOCK_KEY,)
