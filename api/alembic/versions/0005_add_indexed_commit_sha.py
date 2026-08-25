"""add registered_repos.indexed_commit_sha

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-25

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("registered_repos", sa.Column("indexed_commit_sha", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("registered_repos", "indexed_commit_sha")
