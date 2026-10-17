---
description: "Index freshness, checkout lifecycle, concurrency"
paths:
  - "rag/**"
  - "shared/repolace_shared/git/workspace.py"
  - "pipeline/**"
---

## Index freshness protocol (decided)

When a task starts work on an issue:

1. Read the repo's last indexed commit (`registered_repos.indexed_commit_sha`).
2. If it equals the checkout's current commit, the index is current — use it as is.
3. Otherwise index incrementally from that commit: `git diff <indexed_sha> <current_sha>`, then delete and re-chunk **only** the files that changed. Untouched files keep their existing chunks and embeddings.

Implemented in `rag/retrieval/index.py::reindex_if_stale`, single-flight per repo via a transaction-scoped advisory lock.

## Checkout lifecycle (decided)

**One ephemeral clone per task. No persistent clone cache.**

At the start of a task: clone the repo to a temp directory, index from it, let the agents work in it, and delete it when the task ends. Nothing is kept between tasks.

Why this works despite incremental indexing needing the previously-indexed commit: **chunks and embeddings live in Postgres, not in the checkout.** Throwing the tree away does not throw the index away, so re-cloning still skips re-embedding every unchanged file — which is the expensive part. And a *full* clone contains complete history, so `git diff <indexed_sha> <current_sha>` resolves in a brand-new clone. Persistence would only save download time, not correctness.

The one hard constraint: **the clone must not be shallow.** `--depth 1` cannot resolve the previously-indexed commit, so every index would silently degrade to a full reindex and `indexed_commit_sha` would never save any work. If clone cost becomes a problem on large repos, the thing to reach for is a partial clone (`--filter=blob:none`, paired with `--no-renames` on the diff so rename detection does not pull blobs back down): full commit history, deferred file contents. Measure before adopting — untested here.

**Ordering within a task matters.** Index first, from the clean checkout, *before* any agent edits it. Otherwise a reindex would capture the agent's own uncommitted work as if it were repo state and write it into the index.

Because each task gets its own clone, two concurrent tasks on one repo are isolated at the filesystem level for free. The advisory lock in `reindex_if_stale` is still needed — it protects the shared `code_chunks` rows in Postgres, which separate checkouts do not.

When the previously-indexed commit is genuinely unreachable — force-push, or an upstream `gc` — `reindex_if_stale` catches the failed diff and falls back to a full reindex rather than erroring permanently.

**Settled since (this was "still open"):** Verify runs against a copy, never the checkout. Verify executes LLM-generated code and the repo's own test suite, either of which can mutate or delete files, and the copy is what omits `.git` (see "Byte-identical export" under "Verify sandbox" in `.claude/rules/verify-sandbox.md`). The checkout also no longer contains the installation token (see "Branch and PR flow within a task" in `.claude/rules/git-and-pr-flow.md`).

Implemented in `shared/repolace_shared/git/workspace.py::task_workspace`, an async context manager that clones into a temp directory and deletes it on exit. Cleanup failures are logged, never raised: losing a temp directory is a disk leak worth seeing, but it must not replace the exception explaining why the task failed. Expect that path to get exercised once the sandbox runs as root and leaves files the worker cannot delete.

## Concurrency (decided)

Checkouts are per task, so two concurrent tasks on one repository are isolated on the filesystem for free; the shared state is the `code_chunks` rows. **The indexing-lock question is resolved with a bounded wait inside the pipeline, not with the runner's scheduling.** `reindex_if_stale` takes a *try*-lock (`pg_try_advisory_xact_lock`), so a second task on one repository is refused at once with `RepoIndexInProgress`, and that is the *normal* case for a benchmark (three runs of one instance). `_index` in `run.py` retries in 10 s steps up to a 20 min cap and then fails the stage with a message saying so; when the wait ends the index is normally current, so the task's own pass finds nothing to do. **A session must not be reused across `reindex_if_stale` calls:** the function owns its transaction (it commits, and rolls back on failure, because the lock is transaction-scoped), and `rollback()` expires every loaded ORM instance, so touching an attribute afterwards raises `MissingGreenlet` outside the greenlet; `_index` opens a fresh session per attempt. **Product limit, recorded, not fixed:** the index is per repository, not per commit (`registered_repos.indexed_commit_sha` is a single value), so two concurrent tasks on *different branches of one repository* see each other's index, which at best costs reindexing and at worst retrieves chunks from the other branch. Benchmark repositories have one branch (`main`), which is why it does not bite there.

**Worker concurrency and recovery (Phase 2).** `WorkerSettings.concurrency` (default 3) prefork children, `worker_prefetch_multiplier=1`, so one task per child and nothing parked behind a long one. Two beat jobs, `repolace.sweep_queued` and `repolace.reap_abandoned`, are routed to a **separate `repolace.maintenance` queue**: with every child busy on a multi-hour task they would otherwise never run. The **sweeper** republishes `queued` rows older than 5 minutes with `eval_run_id IS NULL`, at most once per 15 minutes per task (a Redis key `repolace:swept:<task id>`, `SET NX EX 900`), at most 50 per tick, with `retry=False`, and **aborts the tick on the first broker or Redis error** (logged once) so an outage cannot become a publish storm; a duplicate that does get through is harmless (same Celery id, conditional claim). The **reaper** fails a `running` row whose `heartbeat_at` is older than 5 minutes (or NULL with `started_at` older than 5 minutes) as `runner: abandoned`. The staleness test is part of the UPDATE's own WHERE clause (`tasks_recovery.fail_if_heartbeat_stale`, `RETURNING`), so a heartbeat landing between its SELECT and its write leaves the row running, and it removes the task's `repolace-<hex12>-*` containers only for rows that UPDATE failed. It reaches only containers on its own docker host, so the maintenance worker must run on the host of the task workers' daemon. It warns (`worker.reaper.long_stale`) when rows are stale for more than 3x the threshold, which means the maintenance worker was down. Beat messages carry `expires: 60`. No index on `heartbeat_at` was added: the reaper reads only `running` rows, at most about K of them. `WORKER_ROLE` (`task`, the default, or `maintenance`) gates the machinery: only the task role builds `Resources`, preflights the models, warms the embedder and sweeps orphans; the maintenance worker needs only the database, Redis and docker. Task children are recycled by `worker_max_memory_per_child` (4,000,000 KiB) and `worker_max_tasks_per_child` (50). At start the task role logs its `task_time_limit` and the RabbitMQ `consumer_timeout` the broker must exceed. On start each task worker deletes `repolace-task-*` directories under `WORKSPACE_PARENT_DIR` older than the hard time limit.

