"""add subscriptions.cancel_at

A cancellation made in the Stripe Customer Portal leaves the subscription `active` with a `cancel_at`
timestamp (the end of the paid period); the account page needs it to say that Pro ends. Nullable and
additive: existing rows stay NULL, which reads as "nothing pending".

The downgrade drops only the column added here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("cancel_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriptions", "cancel_at")
