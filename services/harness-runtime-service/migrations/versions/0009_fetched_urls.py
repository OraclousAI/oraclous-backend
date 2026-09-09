"""the run's fetch registry on harness_executions (#975 §CITE cite-by-reference)

Revision ID: 0009_fetched_urls
Revises: 0008_served_citation_ids
Create Date: #975

Adds one additive JSONB column, ``fetched_urls``: every http(s) URL this run's own segment really
fetched (tool harvests, ``prior_fetched_urls``, a mined ``person_supplied_text``), first-seen order,
deduplicated, capped at the loop's own ``_MAX_FETCHED_URLS`` (2000). It is the registry cite-by-
reference numbers ``[Sn]`` markers against — a citation resolves only if it names an entry in here,
which is the property that makes a fabricated link detectable (and strippable) after the fact.

Defaulted to ``'[]'`` and NOT NULL, the SAME shape ``served_citation_ids`` (0008) has and for the
same reason: the acceptance pass reads this column on every run, and a NULL would make "fetched
nothing" indistinguishable from "not recorded". A pre-#975 run is backfilled to the empty list,
which is honest — those runs registered no fetch, because nothing minted any.

The column holds opaque URL strings the run's own tool calls (or a caller-vouched seed) produced,
never source content, so it carries no tenant data of its own beyond what the run's steps already
persist; org isolation is unchanged (reads filter ``organisation_id``, plus the forced-RLS backstop
from 0006).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_fetched_urls"
down_revision = "0008_served_citation_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harness_executions",
        sa.Column(
            "fetched_urls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("harness_executions", "fetched_urls")
