"""add task_checkpoints, the paused status, tasks.paused_reason, resume_count and stop_class

A task stopped by **unavailability** (a provider refusing calls past the retry window, a dead
process) can continue from where it was; one the agent or model ended cannot. This is the storage for
that, additive throughout.

* `task_status` gains `paused`. A task row is never made `queued` again; `paused` is the one
  non-terminal state a stopped row may be in, and a resumer claims it `paused` -> `running`.
* `tasks.paused_reason` (set only while paused, `ck_tasks_paused_reason_needs_paused`),
  `tasks.resume_count` (0 on every existing row) and `tasks.stop_class`
  (`unavailable` or `design`, `ck_tasks_stop_class`; NULL on every existing row, which is correct:
  none of them records why it stopped in this sense).
* `task_checkpoints`: one row per task holding the agent's transcript and counters, the edited tree as
  two patches, the active seconds so far and the provenance of the run. It is replaced as the task
  moves, and cascades with the task.

`ALTER TYPE ... ADD VALUE` runs in an autocommit block of its own: the CHECK below could not name a
value added in the same transaction ("unsafe use of new value"), and nothing before the block needs it.

The downgrade drops the table and the columns; a Postgres enum cannot lose a value, so `paused` is
removed by rebuilding the type, and any `paused` row is moved to `failed` first so the cast succeeds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES_WITHOUT_PAUSED = ("queued", "running", "pr_opened", "completed", "conflicting", "failed")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'paused'")

    op.add_column("tasks", sa.Column("paused_reason", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("stop_class", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_tasks_paused_reason_needs_paused", "tasks", "paused_reason IS NULL OR status = 'paused'"
    )
    op.create_check_constraint(
        "ck_tasks_stop_class", "tasks", "stop_class IS NULL OR stop_class IN ('unavailable', 'design')"
    )

    op.create_table(
        "task_checkpoints",
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agent_state", postgresql.JSONB(), nullable=False),
        sa.Column("base_sha", sa.String(), nullable=False),
        sa.Column("scored_patch", sa.LargeBinary(), nullable=False, server_default=""),
        sa.Column("delta_patch", sa.LargeBinary(), nullable=False, server_default=""),
        sa.Column("agent_seconds_used", sa.Float(), nullable=False, server_default="0"),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    op.drop_table("task_checkpoints")
    op.drop_constraint("ck_tasks_stop_class", "tasks", type_="check")
    op.drop_constraint("ck_tasks_paused_reason_needs_paused", "tasks", type_="check")
    op.drop_column("tasks", "stop_class")
    op.drop_column("tasks", "resume_count")
    op.drop_column("tasks", "paused_reason")

    # A paused row has no counterpart in the old type, and `failed` is the honest one: it stopped and
    # nothing will continue it. The partial index, the default and 0016's CHECK all mention the column's
    # type, so each is dropped around the rebuild and put back after.
    op.execute("UPDATE tasks SET status = 'failed' WHERE status = 'paused'")
    op.drop_constraint("ck_tasks_conflict_reason_needs_conflicting", "tasks", type_="check")
    op.execute("DROP INDEX IF EXISTS ix_tasks_active")
    op.execute("ALTER TABLE tasks ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE task_status RENAME TO task_status_old")
    postgresql.ENUM(*_STATUSES_WITHOUT_PAUSED, name="task_status").create(op.get_bind())
    op.execute("ALTER TABLE tasks ALTER COLUMN status TYPE task_status USING status::text::task_status")
    op.execute("DROP TYPE task_status_old")
    op.create_check_constraint(
        "ck_tasks_conflict_reason_needs_conflicting", "tasks", "conflict_reason IS NULL OR status = 'conflicting'"
    )
    op.execute("ALTER TABLE tasks ALTER COLUMN status SET DEFAULT 'queued'")
    op.execute("CREATE INDEX ix_tasks_active ON tasks (created_at, id) WHERE status IN ('queued', 'running')")
