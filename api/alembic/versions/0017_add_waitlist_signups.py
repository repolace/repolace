"""add waitlist_signups

The public site's waitlist form writes one row per address. Additive: a new table, nothing
existing is touched. `email` is unique and stored lower-cased by the writer; the length check
mirrors RFC 5321's 254-character limit so a bad writer cannot store an unbounded string.

The downgrade drops only what this migration added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "waitlist_signups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("source", sa.String(), server_default="site", nullable=False),
        sa.Column("consent", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("char_length(email) <= 254", name="ck_waitlist_signups_email_length"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )


def downgrade() -> None:
    op.drop_table("waitlist_signups")
