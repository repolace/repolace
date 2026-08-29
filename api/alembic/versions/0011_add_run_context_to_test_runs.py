"""add task_test_runs.collected_files, conftests, fingerprint, stdout_tail

`SuiteResult` carries four fields the table had no column for, and two of them
are read by the scoring rule rather than merely nice to have:

* `collected_files` (with `conftests`) is what `disqualifying_paths` joins
  against. The path heuristic alone neither catches a repository whose tests
  live in `tests.py` nor spares a shipped module like `django/test/client.py`;
  what pytest actually collected is authoritative for that repository and
  nothing else is.
* `fingerprint` -- rootdir, the watched ini options, the registered plugins --
  is what `fingerprint_changed` refuses on. Node ids are relative to rootdir and
  `filterwarnings` decides whether a warning is an error, so without it an agent
  can turn a real failure into a real pass with a diff that touches no test.

Without these columns a stored run cannot be rescored, only re-run -- which
defeats the reason 0007 stores raw sets rather than derived lists, and the
reason 0009 and 0010 each added one back. `stdout_tail` is the plainer case: it
is the only human-readable account of an unscoreable run.

The table is still empty at the moment this lands -- the pipeline gains its
Verify stage in the same change -- so this is the cheapest it will ever be.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_test_runs",
        sa.Column(
            "collected_files", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"
        ),
    )
    op.add_column(
        "task_test_runs",
        sa.Column("conftests", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
    )
    op.add_column(
        "task_test_runs",
        sa.Column(
            "fingerprint", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "task_test_runs",
        sa.Column("stdout_tail", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("task_test_runs", "stdout_tail")
    op.drop_column("task_test_runs", "fingerprint")
    op.drop_column("task_test_runs", "conftests")
    op.drop_column("task_test_runs", "collected_files")
