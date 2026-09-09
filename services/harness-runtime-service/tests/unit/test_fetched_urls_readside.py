"""#975 (§CITE cite-by-reference) — `HarnessExecutionOut.fetched_urls`, read-side round trip.

Unlike `unverified_links` (#944, `test_link_provenance_readside.py`), `fetched_urls` is NOT derived
from the trace — it is a stored column, the same posture `served_citation_ids` (#743) has. So the
read-side contract here is the simpler one that field already sets: round-trip the value the row
carries, and — mirroring `test_link_provenance_readside.py`'s back-compat test for a pre-#944 trace
— a row from before this column existed (no key at all) still validates and reports the empty list,
never crashing the read and never confusing "fetched nothing" with "not recorded".

RED until `HarnessExecutionOut` declares the field ([impl] slice I3, `schema/harness_schemas.py`).
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.unit]

_A = "https://example.com/a"
_B = "https://example.com/b"


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "organisation_id": uuid.uuid4(),
        "harness_id": uuid.uuid4(),
        "harness_name": "linker",
        "content_hash": None,
        "status": "SUCCEEDED",
        "output": "done",
        "error_type": None,
        "error_message": None,
        "iterations": 1,
        "total_tokens": 0,
        "steps": [],
        "created_at": None,
    }
    base.update(overrides)
    return base


def test_fetched_urls_round_trips_from_a_stored_row() -> None:
    from oraclous_harness_runtime_service.schema.harness_schemas import HarnessExecutionOut

    out = HarnessExecutionOut.model_validate(_row(fetched_urls=[_A, _B]))
    assert out.fetched_urls == [_A, _B]


def test_a_pre_column_row_with_no_fetched_urls_key_reports_the_empty_list() -> None:
    """Back-compat, the `served_citation_ids`/`driving_signals`/`simulated` posture: a row
    persisted before this column existed carries no `fetched_urls` key at all, and the read must
    not crash — it reports the honest answer, an empty registry, never `None`."""
    from oraclous_harness_runtime_service.schema.harness_schemas import HarnessExecutionOut

    out = HarnessExecutionOut.model_validate(_row())
    assert out.fetched_urls == []


def test_fetched_urls_defaults_to_empty_never_none_when_omitted_from_construction() -> None:
    """A caller reads this on every run (the acceptance pass, a console render); `None` would make
    "the registry was empty" indistinguishable from "the harness never recorded it"."""
    from oraclous_harness_runtime_service.schema.harness_schemas import HarnessExecutionOut

    out = HarnessExecutionOut(**_row())  # type: ignore[arg-type]
    assert out.fetched_urls == []
    assert out.fetched_urls is not None
