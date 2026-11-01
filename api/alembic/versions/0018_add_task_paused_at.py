"""add tasks.paused_at and put paused rows in ix_tasks_active

`paused_at` is when the row last became `paused`. It is a gate, not a log: the sweeper republishes a paused
row only that long after it paused, and a resume claim refuses a row paused more recently than the sweeper's
minimum age, so a duplicate broker message for a just-paused row cannot start a resume into a provider that is
still down. It is separate from `updated_at` on purpose: a resume that stopped again before the agent ran made
no progress, and must not postpone the pause's expiry (`updated_at`) just because the gate restarted.

`ix_tasks_active` now includes `paused`. The sweeper's query reads queued rows by `created_at` and paused rows
by `paused_at`, and without the status in the partial predicate it would scan the whole table for the paused ones.
Rebuilt the way 0015 builds it: concurrently, after dropping any invalid leftover of an interrupted build.

The downgrade restores the two-status index and drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rebuild_active(statuses: str) -> None:
    op.drop_index("ix_tasks_active", table_name="tasks", if_exists=True, postgresql_concurrently=True)
    op.create_index(
        "ix_tasks_active",
        "tasks",
        ["created_at", "id"],
        postgresql_concurrently=True,
        postgresql_where=sa.text(f"status IN ({statuses})"),
    )


def upgrade() -> None:
    op.add_column("tasks", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    # Rows already paused (migration 0017 is days old at most) are gated from when they were last written.
    op.execute("UPDATE tasks SET paused_at = updated_at WHERE status = 'paused'")
    with op.get_context().autocommit_block():
        _rebuild_active("'queued', 'running', 'paused'")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _rebuild_active("'queued', 'running'")
    op.drop_column("tasks", "paused_at")
