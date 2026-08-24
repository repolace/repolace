"""add missing indexes and registered_repos.is_active server default

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-25

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_tasks_repo_id_created_at", "tasks", ["repo_id", sa.text("created_at DESC")]
    )
    op.create_index("ix_registered_repos_installation_id", "registered_repos", ["installation_id"])
    op.alter_column("registered_repos", "is_active", server_default=sa.true())


def downgrade() -> None:
    op.alter_column("registered_repos", "is_active", server_default=None)
    op.drop_index("ix_registered_repos_installation_id", table_name="registered_repos")
    op.drop_index("ix_tasks_repo_id_created_at", table_name="tasks")
