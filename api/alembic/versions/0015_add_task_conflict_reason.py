"""add tasks.conflict_reason

Why a `conflicting` task opened no pull request: the target branch moved while the
task ran and the merge onto the scored commit could not be completed (a conflict
that is not plain text, or one the resolver could not settle). A column of its own
because `error_message` keeps exactly one meaning, "repolace broke", and a CHECK so
the reason can only sit beside the status it explains.

Nullable and NULL on every existing row, which is correct: nothing set
`conflicting` before this. The downgrade drops only what this migration added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("conflict_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_tasks_conflict_reason_needs_conflicting",
        "tasks",
        "conflict_reason IS NULL OR status = 'conflicting'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tasks_conflict_reason_needs_conflicting", "tasks", type_="check")
    op.drop_column("tasks", "conflict_reason")
