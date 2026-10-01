"""add llm_calls, tasks.issue_body and the benchmark-row columns

The gateway records every model call, and the real agent needs the issue text
it is meant to fix. Two tables' worth of change, one migration, because neither
is useful without the other: an agent with no `issue_body` has nothing to
prompt with, and a gateway with nowhere to write cannot honour the rule that
cost is measured from the first run.

* `llm_calls` holds one row per call, request and response included. Stored raw
  for the reason 0007 stores raw test sets: the benchmark's headline is
  success rate, cost and latency, and a cost that was not captured at the time
  cannot be recovered by re-pricing later without re-running -- nor can a
  trajectory be inspected once the clone it came from is gone. The cost of that
  choice is that the table holds repo source verbatim, which matters for
  private repositories.
* `tasks.issue_body` is untrusted input (anyone can file an issue on a public
  repo) and is stored so the run can be audited and re-prompted.
* `tasks.eval_run_id`, `instance_id` and `run_index` are all nullable and
  populated only by the eval harness, so benchmark rows are queryable from
  `tasks` itself rather than from a second store that could drift.

`llm_calls.cost_usd` carries eight decimal places where `tasks.cost_usd` carries
four: a single cheap-model call prices in the thousandths of a cent, and a
column that rounds each of them to zero would sum to nothing.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("issue_body", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("eval_run_id", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("instance_id", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("run_index", sa.Integer(), nullable=True))

    op.create_table(
        "llm_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(14, 8), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "request", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("attempt IS NULL OR attempt >= 0", name="ck_llm_calls_attempt_non_negative"),
    )
    op.create_index("ix_llm_calls_task_id_created_at", "llm_calls", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_task_id_created_at", table_name="llm_calls")
    op.drop_table("llm_calls")

    op.drop_column("tasks", "run_index")
    op.drop_column("tasks", "instance_id")
    op.drop_column("tasks", "eval_run_id")
    op.drop_column("tasks", "issue_body")
