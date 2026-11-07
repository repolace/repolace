"""add tasks.user_id

The plan allowance counts "tasks this user started this month", which needs an owner on the task.
Nullable and additive: existing rows, benchmark rows and tasks started with no signed-in user stay
NULL. ON DELETE SET NULL keeps a task (and its cost evidence) when an account is deleted.

The downgrade drops only the index and the column added here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_tasks_user_id_users", "tasks", "users", ["user_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_tasks_user_created", "tasks", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_tasks_user_created", table_name="tasks")
    op.drop_constraint("fk_tasks_user_id_users", "tasks", type_="foreignkey")
    op.drop_column("tasks", "user_id")
