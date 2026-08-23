"""create github_installations and registered_repos

Revision ID: 0001
Revises:
Create Date: 2026-08-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "github_installations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.String(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("account_type", sa.String(), nullable=False),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "registered_repos",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("github_repo_id", sa.BigInteger(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("default_branch", sa.String(), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["installation_id"], ["github_installations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_repo_id"),
    )


def downgrade() -> None:
    op.drop_table("registered_repos")
    op.drop_table("github_installations")
