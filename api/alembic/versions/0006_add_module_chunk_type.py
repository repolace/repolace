"""add 'module' to the chunk_type enum

Module-level code (imports, constants, module docstrings) is now chunked, so
it needs its own chunk_type value.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-25

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = ("function", "method", "class_skeleton")


def upgrade() -> None:
    op.execute("ALTER TYPE chunk_type ADD VALUE IF NOT EXISTS 'module'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum, so rebuild the type. Rows
    # using the removed value would violate the new type, so they are deleted
    # first — they are derived data and are restored by a reindex.
    op.execute("DELETE FROM code_chunks WHERE chunk_type = 'module'")

    old_values = ", ".join(f"'{value}'" for value in _OLD_VALUES)
    op.execute(f"CREATE TYPE chunk_type_old AS ENUM ({old_values})")
    op.execute(
        "ALTER TABLE code_chunks ALTER COLUMN chunk_type "
        "TYPE chunk_type_old USING chunk_type::text::chunk_type_old"
    )
    op.execute("DROP TYPE chunk_type")
    op.execute("ALTER TYPE chunk_type_old RENAME TO chunk_type")
