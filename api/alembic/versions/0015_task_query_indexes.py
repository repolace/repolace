"""index the queries that read more of `tasks` and `llm_calls` than they need

A health check found three query shapes whose cost grows with history rather than with the
rows they return. Nothing here adds a column or touches a row: three indexes are created and
one made redundant is dropped.

* `ix_tasks_repo_created_id` on `tasks (repo_id, created_at DESC, id DESC)`. The keyset task
  list orders by `(created_at DESC, id DESC)` and pages with `(created_at, id) < (:c, :i)`.
  With the old two-column index the planner can bound only `created_at <= :c`, so every page
  re-reads the ties at that timestamp and filters them one by one. With `id` in the key the
  row comparison itself is an index condition, and `ORDER BY ... LIMIT` stops after one page.
  The two columns after `repo_id` share a direction on purpose: Postgres only turns a row
  comparison into an index condition when the columns agree.
* `ix_tasks_repo_id_created_at` is **dropped**: its columns, order and direction are the
  prefix of the new index, so every scan it served (the estimate's window, the enqueue's
  in-flight check, the cascade from `registered_repos`) is served by the new one, and it was an
  extra index write on every task insert. The plan tests assert the estimate still reads an
  index after the drop.
* `ix_tasks_active` on `tasks (created_at, id) WHERE status IN ('queued', 'running')`. The
  worker's sweeper (`queued`, oldest first) and reaper (`running`, stale heartbeat) run every
  minute and otherwise read the whole table to find a handful of rows. Partial, because those
  rows are a tiny and bounded share of the table whatever its age, and ordered as the
  sweeper sorts. `heartbeat_at` is deliberately not in the key: it changes every ~30 s per
  running task, and an indexed column would turn each beat into an index write (and rule out
  heap-only updates).
* `ix_llm_calls_task_created_desc` on `llm_calls (task_id, created_at DESC)`. The estimate's
  fallback takes the latest call per task with `DISTINCT ON (task_id) ... ORDER BY task_id,
  created_at DESC`; an ascending index delivers `task_id` in order but not the newest call
  first inside each task, so it needs a sort. The ascending `ix_llm_calls_task_id_created_at`
  stays: the per-task cost sum and the relationship's `order_by created_at` use it.

Every build is `CREATE INDEX CONCURRENTLY`, so a populated `tasks` or `llm_calls` table stays
writable while it runs, which means it cannot run inside the migration's transaction;
`autocommit_block()` steps out of it. The price is that a failure part-way is not rolled back:
a cancelled concurrent build leaves an INVALID index that still owns its name, and the
partial run's earlier steps are already committed. So every create is preceded by
`DROP INDEX CONCURRENTLY IF EXISTS` of the same name and the drop of the old index is
`IF EXISTS`, which makes running the revision again after a failure safe.

The new tasks index is created before the old one is dropped, so the list is never without
an index. The downgrade is the reverse, and recreates the old index before dropping the new
one for the same reason.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_STATUSES = "status IN ('queued', 'running')"


def _rebuild(name: str, table: str, columns: Sequence[str | sa.TextClause], **kw) -> None:
    """Create `name` concurrently, first clearing an invalid leftover of an earlier, interrupted build."""
    op.drop_index(name, table_name=table, if_exists=True, postgresql_concurrently=True)
    op.create_index(name, table, list(columns), postgresql_concurrently=True, **kw)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        _rebuild("ix_tasks_repo_created_id", "tasks", ["repo_id", sa.text("created_at DESC"), sa.text("id DESC")])
        op.drop_index("ix_tasks_repo_id_created_at", table_name="tasks", if_exists=True, postgresql_concurrently=True)
        _rebuild("ix_tasks_active", "tasks", ["created_at", "id"], postgresql_where=sa.text(ACTIVE_STATUSES))
        _rebuild("ix_llm_calls_task_created_desc", "llm_calls", ["task_id", sa.text("created_at DESC")])


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index("ix_llm_calls_task_created_desc", table_name="llm_calls", if_exists=True, postgresql_concurrently=True)
        op.drop_index("ix_tasks_active", table_name="tasks", if_exists=True, postgresql_concurrently=True)
        _rebuild("ix_tasks_repo_id_created_at", "tasks", ["repo_id", sa.text("created_at DESC")])
        op.drop_index("ix_tasks_repo_created_id", table_name="tasks", if_exists=True, postgresql_concurrently=True)
