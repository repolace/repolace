"""add user_repositories

Sign-in now covers every repo and task route, and a user sees only the repositories they can access
on GitHub. That set, with whether they can push, is read at sign-in with the user's own token
(`GET /user/installations`, then `GET /user/installations/{id}/repositories`) and stored here,
replaced on every sign-in.

Per repository, not per installation: GitHub lists an installation for a user who can read any one of
its repositories, so storing installations would open every repository in it to that user, and let a
read-only user start tasks that push. `github_repo_id` has no foreign key: a user can sign in before
the installation webhook has registered the repository. Rows go with their user (ON DELETE CASCADE).

Additive: no existing row changes. The downgrade drops the table added here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_repositories",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("github_repo_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("can_push", sa.Boolean(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "github_repo_id"),
    )
    op.create_index("ix_user_repositories_github_repo_id", "user_repositories", ["github_repo_id"])


def downgrade() -> None:
    op.drop_index("ix_user_repositories_github_repo_id", table_name="user_repositories")
    op.drop_table("user_repositories")
