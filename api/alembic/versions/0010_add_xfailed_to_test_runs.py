"""add task_test_runs.xfailed

pytest reports an expected failure as a skip, so the report parser had been
folding the two into one `skipped` set. The scoring rule treats them
oppositely: an xfail is a known bug the repository has written down, so it is
red at baseline and counts toward fail-to-pass; an ordinary skip is not red and
counts toward nothing. Folding them let a baseline `importorskip` that started
passing score a task PASSED with nothing red at the base commit at all.

Stored rather than derived, for the reason 0009 gives for the sets it added:
the point of keeping raw sets is that a revised rule can rescore existing runs
without re-running anything, and "was this an xfail or an ordinary skip" is
precisely the question this revision asked. A dropped set cannot be recovered.

The table is still empty -- nothing constructs a TaskTestRun yet -- so this is
the cheapest moment it will ever be to add the column.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_test_runs",
        sa.Column("xfailed", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("task_test_runs", "xfailed")
