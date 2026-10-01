"""add the agent's outputs to tasks, the index strategy to repos, and idempotent eval rows

The real agent and the benchmark harness need six things the schema cannot yet
hold. All are additive (four nullable columns, two CHECK constraints, one partial
index), so nothing that writes today has to change and the downgrade loses only
what this migration itself added. The eval-columns CHECK constrains rows no
writer in the codebase produces yet (0012's eval columns are only written by
tests), so adding it cannot fail on existing data.

* `tasks.patch_diff` is the final diff (`review_diff()`), written for every task
  that got as far as the agent. The clone is deleted when the task ends and a
  benchmark task that does not score PASSED never pushes, so without this the
  patch is unrecoverable -- and a verdict nobody can re-examine is one nobody
  should believe. `patch_sha` stays a git sha; it cannot carry the content.
* `tasks.score_reason` is `Score.reason`, which `score()` computes today and the
  pipeline discards. The report needs it, and rescoring later is lossy because
  `baseline_files` (the set that spares a shipped `django/test/client.py`) is not
  stored.
* `tasks.agent_stop_reason` is why the agent loop ended, as a string guarded by a
  CHECK constraint rather than a Postgres enum. Migrations 0007 and 0009 showed
  what an enum costs: a value cannot be removed without rebuilding the type
  (0007), and one that is added (`ALTER TYPE ... ADD VALUE`, 0009) cannot be used
  in the transaction that added it. A reason this young will grow members; a
  CHECK is dropped and recreated in one statement. The list has eight members
  from the start, including `max_attempts` -- the agent used every attempt and
  the last scored one still had visible regressions or collection errors --
  because without it "submitted clean" and "ran out of attempts still red" both
  read `submitted`, and a report that cannot tell them apart is the one
  distribution the benchmark most needs. The reason is a separate column from
  `error_message` because CLAUDE.md reserves `error_message` for FAILED
  ("exactly one meaning") -- an agent that ran out of budget did not break
  repolace, so its reason cannot live there.
* `registered_repos.index_strategy` is the embedding strategy the repo's index
  was built with; NULL means the legacy `truncate`. Without it, changing the
  strategy on a repo whose index is already current never reindexes it, because
  `reindex_if_stale` only compares commits -- so old and new embeddings mix in
  one index and retrieval quality is silently a blend of two experiments.
* `uq_tasks_eval_instance_run` is a partial unique index on
  `(eval_run_id, instance_id, run_index)` for benchmark rows only. It makes the
  harness's enqueue idempotent: re-running it after a crash cannot create a
  second row for the same instance and run, which would be scored twice. Partial
  because every product task has a NULL `eval_run_id`, and those must stay free
  to repeat.
* `ck_tasks_eval_columns_together` requires those three columns to be all NULL
  or all set. The index above only protects a row where all three are non-NULL,
  because NULLs are distinct in a Postgres unique index: an enqueue that
  forgot `run_index` would insert a row the index never compares against any
  other, and the idempotency it exists for would silently be gone. The CHECK
  turns that omission into an insert error at the call that made it. It is
  deliberately a CHECK and not `NULLS NOT DISTINCT` (PG15+): that would make two
  *product* rows, which are all-NULL, collide.

The CHECK list is spelled out here rather than imported from
`repolace_shared.db.models.AGENT_STOP_REASONS`. A migration is a snapshot of what
the schema was at this revision; importing the live constant would rewrite
history the next time a reason is added, and the follow-up migration that adds
it would then be recreating a constraint this one had already created with the
new list. The models test iterates the live constant against the migrated
database, which is what catches the two drifting.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AGENT_STOP_REASONS = (
    "submitted",
    "step_cap",
    "budget_usd",
    "budget_calls",
    "budget_wall",
    "llm_error",
    "no_change",
    "max_attempts",
)


def upgrade() -> None:
    op.add_column("tasks", sa.Column("patch_diff", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("score_reason", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("agent_stop_reason", sa.String(), nullable=True))
    reasons = ", ".join(f"'{reason}'" for reason in _AGENT_STOP_REASONS)
    op.create_check_constraint(
        "ck_tasks_agent_stop_reason",
        "tasks",
        f"agent_stop_reason IS NULL OR agent_stop_reason IN ({reasons})",
    )

    op.add_column("registered_repos", sa.Column("index_strategy", sa.String(), nullable=True))

    # Before the unique index, so the index is never the only thing standing
    # between an insert and a half-keyed benchmark row.
    op.create_check_constraint(
        "ck_tasks_eval_columns_together",
        "tasks",
        "(eval_run_id IS NULL) = (instance_id IS NULL) AND (eval_run_id IS NULL) = (run_index IS NULL)",
    )

    op.create_index(
        "uq_tasks_eval_instance_run",
        "tasks",
        ["eval_run_id", "instance_id", "run_index"],
        unique=True,
        postgresql_where=sa.text("eval_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_tasks_eval_instance_run", table_name="tasks")
    op.drop_constraint("ck_tasks_eval_columns_together", "tasks", type_="check")

    op.drop_column("registered_repos", "index_strategy")

    op.drop_constraint("ck_tasks_agent_stop_reason", "tasks", type_="check")
    op.drop_column("tasks", "agent_stop_reason")
    op.drop_column("tasks", "score_reason")
    op.drop_column("tasks", "patch_diff")
