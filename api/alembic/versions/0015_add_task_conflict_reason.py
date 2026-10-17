"""add tasks.conflict_reason

Set when a task ends `conflicting`: the target branch moved while the task ran and the
change could not be merged onto it (a conflict a text resolver cannot edit, a resolver that
gave up, a rewritten target). A short host-written sentence, never model output.

`ck_tasks_conflict_reason_needs_conflicting` keeps a reason off every other status, so the
column cannot be read as a second error field. The other direction (every `conflicting` row has
a reason) is the pipeline's job and its tests' rather than a constraint, because existing UI
fixtures seed a `conflicting` row with no reason. No existing row has a reason, so none can
violate it.

The downgrade drops only what this migration added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONFLICT_REASON_CHECK = "conflict_reason IS NULL OR status = 'conflicting'"


def upgrade() -> None:
    op.add_column("tasks", sa.Column("conflict_reason", sa.Text(), nullable=True))
    op.create_check_constraint("ck_tasks_conflict_reason_needs_conflicting", "tasks", CONFLICT_REASON_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_tasks_conflict_reason_needs_conflicting", "tasks", type_="check")
    op.drop_column("tasks", "conflict_reason")
