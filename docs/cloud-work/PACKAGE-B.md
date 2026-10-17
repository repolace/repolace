# Package B: database performance

Read `COMMON.md` first. Model: Claude Opus 5.5, started in plan mode.
Branch: `cloud/b-db-performance`, from `main`. PR target: `main`.
**Migration number: this package takes `0015`.** Package C also needs a migration; it takes the next number
after yours and rebases when you merge, so write yours on top of `0014`.

## Goal

A health check found queries that read far more than they need, all invisible at today's scale and all growing
with history. Fix them with indexes and bounded queries, and prove the planner uses the indexes. No behaviour
change: every response and every row stays identical.

## Do exactly these

1. **Migration `0015_task_query_indexes.py`** (and the same indexes declared on the models in
   `shared/repolace_shared/db/models.py`, because `alembic check` must stay clean):
   * `ix_tasks_repo_created_id` on `tasks (repo_id, created_at DESC, id DESC)`: the keyset task list orders and
     compares on `(created_at, id)` (`api/repolace_api/routes/repos.py::query_task_page`), which the existing
     `(repo_id, created_at DESC)` index cannot serve as a range boundary. Create it with
     `CREATE INDEX CONCURRENTLY` inside `op.get_context().autocommit_block()`. Keep or drop the old index
     only if you can show the new one fully replaces it for every query that used it.
   * `ix_tasks_active` on `tasks (created_at, id) WHERE status IN ('queued','running')`: the worker's sweeper
     and reaper (`shared/repolace_shared/tasks_recovery.py`, `worker/repolace_worker/maintenance.py`) scan for
     these two statuses every minute and otherwise read the whole table. (Package A changes the sweeper's SQL
     text; you only supply the index. Do not edit `worker/`.)
   * `ix_llm_calls_task_created_desc` on `llm_calls (task_id, created_at DESC)`: the cost estimate's fallback
     takes the latest call per task with `DISTINCT ON (task_id) ... ORDER BY task_id, created_at DESC`, which an
     ascending index cannot serve.
   * a faithful `downgrade()` (indexes dropped in reverse order). Follow how `0013` and `0014` do it.
2. **Bound the cost estimate** (`routes/repos.py::compute_estimate`, ~lines 274-335): the `model` fallback
   scans every priced task in the system and runs `percentile_cont` over all of them. Limit it to the most
   recent 30 qualifying tasks, as the `repo` path already does (`ESTIMATE_WINDOW`). Add a test with more than 30
   tasks that shows only the latest 30 are used. Behaviour for fewer than 30 is unchanged.
3. **The live detail poll must not reload large columns** (`api/repolace_api/routes/tasks.py::load_task_detail`
   and the `/ui/tasks/{id}/live` and `/ui/tasks/{id}/row` handlers): while a task runs, load the row without
   `patch_diff`, `issue_body` and `score_reason` (use `load_only`/`defer`); load them on the terminal render.
   Prove with a SQLAlchemy `before_cursor_execute` listener in a test that the polling queries do not select them.
4. **Plan tests** (`api/tests/test_api_query_plans_db.py`, `-m db`): seed a few thousand `tasks` and `llm_calls`
   rows, `ANALYZE`, then `EXPLAIN (FORMAT JSON)` the keyset list query, the sweeper's `status IN (...)` query and
   the estimate's latest-call subquery. Planners are not deterministic on tiny tables, so run each with
   `SET LOCAL enable_seqscan = off` and assert the plan uses the **named index**, and also assert the index
   exists in the migrated schema. Say in a comment why `enable_seqscan` is off: the test checks that an index
   *can* serve the query, not that the planner always chooses it.
5. Update `.claude/rules/task-bookkeeping.md` (the schema list) and the "API" part of
   `.claude/rules/api-and-ui.md` (what the estimate and the polling read).

## Acceptance

* `alembic heads` shows one head `0015`; `alembic check` is clean; upgrade then downgrade then upgrade works in
  the `-m db` migration tests (`shared/tests/test_migrations.py` has the pattern; add `0015` to it).
* New tests fail without the change; every existing `api` and `shared` test passes unchanged.
* The JSON and HTML responses are byte-identical for the existing fixtures.

## Must not touch

`worker/`, `infra/`, `pipeline/`, `agents/`, `gateway/`, `verify/`, `eval/` (package A and C own those or they
are off limits). No new columns and no data changes: indexes and query shapes only.
