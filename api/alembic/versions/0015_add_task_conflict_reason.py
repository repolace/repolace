"""add tasks.conflict_reason

Set when a task ends `conflicting`: the target branch moved while the task ran and the
change could not be merged onto it (a conflict a text resolver cannot edit, a resolver that
gave up, a rewritten target). A short host-written sentence, never model output.

`ck_tasks_conflict_reason_iff_conflicting` makes the two inseparable: a `conflicting` row
always says why, and no other status carries a reason. Nothing set `conflicting` before this
migration, so no existing row can violate it.

The downgrade drops only what this migration added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONFLICT_REASON_CHECK = "(status = 'conflicting') = (conflict_reason IS NOT NULL)"


def upgrade() -> None:
    op.add_column("tasks", sa.Column("conflict_reason", sa.Text(), nullable=True))
    op.create_check_constraint("ck_tasks_conflict_reason_iff_conflicting", "tasks", CONFLICT_REASON_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_tasks_conflict_reason_iff_conflicting", "tasks", type_="check")
    op.drop_column("tasks", "conflict_reason")
