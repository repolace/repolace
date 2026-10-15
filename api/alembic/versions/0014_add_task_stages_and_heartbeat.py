"""add task_stages, tasks.heartbeat_at and tasks.claimed_by

Two things the pipeline view and the worker's reaper need, both additive.

* `task_stages` holds one row per pipeline stage of a task: when it started, when
  it ended and how. The pipeline writes it best-effort, so a missing or
  still-`running` row means a lost write or a dead runner, not a verdict; the
  task row stays the source of truth. `status` is a string behind a CHECK rather
  than a Postgres enum, for the reason 0013 gives for `agent_stop_reason`.
* `tasks.heartbeat_at` is bumped by the running process, and `tasks.claimed_by`
  names it. A `running` row whose heartbeat has gone stale is an abandoned task.
  Both are NULL on every existing row, which is correct: none of them has a
  heartbeating runner.

The downgrade drops only what this migration added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("claimed_by", sa.String(), nullable=True))
    op.create_table(
        "task_stages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('running', 'ok', 'failed')", name="ck_task_stages_status"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_stages_task_id_started_at", "task_stages", ["task_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_task_stages_task_id_started_at", table_name="task_stages")
    op.drop_table("task_stages")
    op.drop_column("tasks", "claimed_by")
    op.drop_column("tasks", "heartbeat_at")
