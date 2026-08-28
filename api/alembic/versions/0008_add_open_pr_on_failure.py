"""add tasks.open_pr_on_failure

Verify can now fail a task: the agent's patch does not make the tests pass
within the retry budget. What should happen then is a product question, not a
technical one -- a PR carrying partial work is useful to some people and noise
to others -- so it is per task.

Default false, so a repolace PR means "the tests passed" unless someone
deliberately opted out. That keeps the strong reading of a PR as the default.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-28

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default is required, not stylistic: the column is NOT NULL and
    # existing rows need a value at the moment it is added.
    op.add_column(
        "tasks",
        sa.Column("open_pr_on_failure", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tasks", "open_pr_on_failure")
