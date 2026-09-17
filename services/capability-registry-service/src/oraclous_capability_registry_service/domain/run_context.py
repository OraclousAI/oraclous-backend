"""The per-run configuration keys a dispatch may assert about itself (domain layer) — pure.

#1130. A tool instance's stored ``configuration`` is shared: a seeded app's sub-harness id is
``uuid.uuid5(app_id, role)``, stable across runs, so every run of that app dispatches through the
SAME instance row, and the row is re-read on every single dispatch
(``tool_execution_service.execute_sync``). Two runs in flight at once therefore cannot both be
described by it — whichever wrote last decides where the other one's output is filed.

So the run says who it is ON the dispatch, and that wins over the stored row. These are the only
keys it may say: a closed vocabulary, not an arbitrary configuration overlay, so the seam can
never become a way to reach a connector setting the instance's owner chose. An unknown key is
REFUSED rather than dropped — a per-run key added upstream and forgotten here then fails loudly
instead of silently reverting to the shared row's value.
"""

from __future__ import annotations

#: ``working_dir`` (#518) the run's trusted working tree, ``graph_id`` (#524) the run's graph,
#: ``precedence`` (#538) the team's hierarchy-of-truth order, and the #728 producer fields — WHO is
#: writing, which ``graph_ingest._producer_config`` reads to stamp an artifact.
PER_RUN_CONFIGURATION_KEYS = frozenset(
    {
        "working_dir",
        "graph_id",
        "precedence",
        "producer_kind",
        "member_role",
        "team_run_id",
        "team_id",
        "execution_id",
        "attempt_id",
        "ordinal",
    }
)
