"""add TaskStatus.COMPLETED, the dropped test-run sets, and patch provenance

Three changes, all cheapest now because `tasks` and `task_test_runs` are still
effectively empty.

1. `completed` — a task where the pipeline ran to the end and the agent's patch
   did not work is not a pipeline failure. Without this value `failed` would
   mean two different things, told apart only by whether `error_message` is
   NULL, which is an unwritten rule nothing enforces. `failed` keeps meaning
   "repolace broke"; read `outcome` for the verdict.

2. `skipped` / `did_not_run` / `collect_failures` on task_test_runs. These are
   already computed by the report parser and then discarded. The stated reason
   for storing raw sets rather than derived lists is that the scoring rule can
   be revised and everything rescored without re-running anything -- and "was
   this a skip or a did-not-run" is exactly the question a revision asks. A
   dropped set cannot be recovered.

3. `tasks.patch_sha` / `tasks.changed_files`. The clone is deleted when the task
   ends, so today a PASSED verdict cannot be re-examined afterwards. The
   benchmark's credibility depends on someone being able to audit a pass, and
   the squashed commit alone does not survive on the agent branch once it is
   deleted.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES_WITHOUT_COMPLETED = ("queued", "running", "pr_opened", "conflicting", "failed")
_RUN_SET_COLUMNS = ("skipped", "did_not_run", "collect_failures")


def upgrade() -> None:
    # Postgres 12+ permits ADD VALUE inside a transaction; the new value merely
    # cannot be *used* in the same one, and nothing here uses it.
    op.execute("ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'completed'")

    for name in _RUN_SET_COLUMNS:
        op.add_column(
            "task_test_runs",
            sa.Column(name, postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        )

    # Nullable rather than defaulted: NULL means "never got that far", which is
    # a different thing from a patch that changed no files.
    op.add_column("tasks", sa.Column("patch_sha", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("changed_files", postgresql.ARRAY(sa.String()), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "changed_files")
    op.drop_column("tasks", "patch_sha")
    for name in reversed(_RUN_SET_COLUMNS):
        op.drop_column("task_test_runs", name)

    # Postgres cannot drop a value from an enum, so the type is rebuilt. Rows
    # using the removed value would violate the new type; `completed` means the
    # pipeline finished and the agent lost, so `failed` is the closest surviving
    # meaning -- lossy, which is why the upgrade is the direction that matters.
    op.execute("ALTER TABLE tasks ALTER COLUMN status DROP DEFAULT")
    values = ", ".join(f"'{value}'" for value in _STATUSES_WITHOUT_COMPLETED)
    op.execute(f"CREATE TYPE task_status_old AS ENUM ({values})")
    op.execute(
        "ALTER TABLE tasks ALTER COLUMN status TYPE task_status_old "
        "USING (CASE WHEN status::text = 'completed' THEN 'failed' ELSE status::text END)"
        "::task_status_old"
    )
    op.execute("DROP TYPE task_status")
    op.execute("ALTER TYPE task_status_old RENAME TO task_status")
    op.execute("ALTER TABLE tasks ALTER COLUMN status SET DEFAULT 'queued'")
