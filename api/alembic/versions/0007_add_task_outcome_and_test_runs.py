"""replace task_status 'merged' with 'pr_opened', add task outcome and test runs

Three related changes, all in service of the benchmark being expressible at all.

1. `merged` was a task state the pipeline could never set. A task's work ends
   when the PR exists; whether it merges is decided by branch protection and
   human reviewers, which CLAUDE.md puts outside repolace's job, and nothing
   subscribes to PR webhooks to find out. Replaced by `pr_opened`.

2. The recorded success criteria score a task as passed / passed_with_test_edit
   / failed, and the headline figure counts only the first. There was nowhere
   to put that, so the project's central claim had no schema behind it.

3. Those criteria are fail-to-pass plus pass-to-pass, which need the baseline
   test results to compare against. task_test_runs keeps one row per run --
   baseline plus each bounded retry -- rather than two columns on tasks.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_STATUSES = ("queued", "running", "pr_opened", "conflicting", "failed")
_OLD_STATUSES = ("queued", "running", "merged", "conflicting", "failed")

task_outcome = postgresql.ENUM(
    "passed", "passed_with_test_edit", "failed", name="task_outcome", create_type=False
)


def _rebuild_task_status(new_values: tuple[str, ...], case_expression: str) -> None:
    """Swap the task_status enum for one with a different member list.

    Postgres can add an enum value but cannot remove one, so the type has to be
    rebuilt. The default is dropped first because it is a value of the old type
    and would block the column's type change.
    """
    values = ", ".join(f"'{value}'" for value in new_values)
    op.execute("ALTER TABLE tasks ALTER COLUMN status DROP DEFAULT")
    op.execute(f"CREATE TYPE task_status_new AS ENUM ({values})")
    op.execute(
        "ALTER TABLE tasks ALTER COLUMN status TYPE task_status_new "
        f"USING ({case_expression})::task_status_new"
    )
    op.execute("DROP TYPE task_status")
    op.execute("ALTER TYPE task_status_new RENAME TO task_status")
    op.execute("ALTER TABLE tasks ALTER COLUMN status SET DEFAULT 'queued'")


def upgrade() -> None:
    _rebuild_task_status(
        _NEW_STATUSES,
        "CASE WHEN status::text = 'merged' THEN 'pr_opened' ELSE status::text END",
    )

    task_outcome.create(op.get_bind(), checkfirst=True)
    op.add_column("tasks", sa.Column("outcome", task_outcome, nullable=True))
    op.add_column("tasks", sa.Column("test_edit_justification", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("test_edit_approved_by", sa.String(), nullable=True))
    op.add_column(
        "tasks", sa.Column("test_edit_approved_at", sa.DateTime(timezone=True), nullable=True)
    )

    # The exception to "no test edits" is the one part of the success criteria
    # that is not machine-checkable, so the database enforces what it can: an
    # unexplained test-edit pass cannot be recorded at all.
    op.create_check_constraint(
        "ck_tasks_test_edit_needs_justification",
        "tasks",
        "outcome IS DISTINCT FROM 'passed_with_test_edit' OR test_edit_justification IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_tasks_test_edit_approval_is_complete",
        "tasks",
        "(test_edit_approved_by IS NULL) = (test_edit_approved_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_tasks_test_edit_approval_needs_test_edit",
        "tasks",
        "test_edit_approved_at IS NULL OR outcome = 'passed_with_test_edit'",
    )

    op.create_table(
        "task_test_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("commit_sha", sa.String(), nullable=False),
        sa.Column("passed", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("failed", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(10, 3), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "attempt", name="uq_task_test_runs_task_id_attempt"),
        sa.CheckConstraint("attempt >= 0", name="ck_task_test_runs_attempt_non_negative"),
    )


def downgrade() -> None:
    op.drop_table("task_test_runs")

    op.drop_constraint("ck_tasks_test_edit_approval_needs_test_edit", "tasks", type_="check")
    op.drop_constraint("ck_tasks_test_edit_approval_is_complete", "tasks", type_="check")
    op.drop_constraint("ck_tasks_test_edit_needs_justification", "tasks", type_="check")
    op.drop_column("tasks", "test_edit_approved_at")
    op.drop_column("tasks", "test_edit_approved_by")
    op.drop_column("tasks", "test_edit_justification")
    op.drop_column("tasks", "outcome")
    task_outcome.drop(op.get_bind())

    _rebuild_task_status(
        _OLD_STATUSES,
        "CASE WHEN status::text = 'pr_opened' THEN 'merged' ELSE status::text END",
    )
