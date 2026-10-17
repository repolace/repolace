# Package A: deployment hardening for the Celery worker and the API container

Read `COMMON.md` first. Budget: **hard stop at about $15**. Model: Claude Sonnet 5.5.
Branch: `cloud/a-deployment-hardening`, from `main`. PR target: `main`.

## Goal

The Celery worker (merged in #31) is correct in its logic but cannot run as deployed: the compose `worker`
service has no credentials and no way to reach the sandbox, RabbitMQ would kill long tasks, and a few
recovery edges are wrong. Make the worker deployable and the recovery paths honest. Nothing here changes
how a task is *processed*; that is `pipeline/` and is not yours.

## Do exactly these, in this order

1. **The `api` container gets its own env file** (`infra/docker-compose.yml`, `api/` settings).
   Today it loads the whole repository `.env`, so provider keys and the GitHub App key sit in an
   internet-facing container's environment. Create `infra/api.env.example` listing only what
   `ApiSettings` reads (`api/repolace_api/config.py`: database, Redis, broker, GitHub App id/slug/private
   key/webhook secret, UI host list). Point the `api` service at `infra/api.env` (gitignored; the example is
   committed), `required: false` as today. Add `infra/api.env` to `.gitignore`. Update `.claude/rules/api-and-ui.md`
   ("Exposure"), which currently says this is not done.
2. **A worker deployment that can run a task** (`infra/docker-compose.yml`, `worker/Dockerfile`):
   * two services from one image: `worker` (`WORKER_ROLE=task`, default queue) and `worker-maintenance`
     (`WORKER_ROLE=maintenance`, `-Q repolace.maintenance`, with `celery beat` for the sweeper and reaper).
     Look at `worker/repolace_worker/celery_app.py` and `tasks.py` for how roles and queues are chosen.
   * the task worker needs: provider/GitHub/gateway settings via its own `infra/worker.env` (+ `.example`,
     gitignored), `git` >= 2.38, `prlimit` (util-linux) and the `docker` CLI in the image, a writable workspace
     volume (`WORKSPACE_PARENT_DIR`), and access to the **rootless** Docker socket
     (`${DOCKER_SOCKET:-/run/user/1000/docker.sock}:/var/run/docker.sock`, with `DOCKER_HOST` set). **Never mount
     the rootful `/var/run/docker.sock`**: it is root-equivalent on the host and defeats the sandbox
     (`.claude/rules/verify-sandbox.md`). Put that warning next to the mount in a comment.
   * the maintenance worker needs only the database, Redis, the broker and the Docker CLI/socket (it removes
     orphaned containers); give it no provider keys.
   * you cannot run compose here. Validate with `docker compose config -q` if the CLI exists, and say so in the
     report: "not started, no daemon".
3. **RabbitMQ `consumer_timeout`** (`infra/rabbitmq.conf`, mounted in the `rabbitmq` service, and
   `worker/repolace_worker/tasks.py::required_consumer_timeout_ms`). RabbitMQ's default 30 minutes closes the
   channel of an `acks_late` task that runs up to ~3 h. `required_consumer_timeout_ms` today returns exactly
   the hard limit; the broker needs *more* than the longest task. Make it hard limit + a documented margin,
   write the value into `rabbitmq.conf` with a comment that points at the function, and add a test that the
   file's value is >= the function's.
4. **Sweeper edges** (`worker/repolace_worker/maintenance.py`): republished messages carry `expires` of about
   one sweeper interval so a backlog does not stack duplicates; the SQL gets a `LIMIT` (a multiple of
   `sweeper_max_per_tick`) instead of loading every old queued id; tests with a fake Redis/broker.
5. **Timeout row message** (`worker/repolace_worker/tasks.py`): when the bound fires, the row reads
   `interrupted: CancelledError` because `run_task`'s cancellation handler writes first, and the later
   `worker: timeout` write is a no-op. Reproduce this in a test first (`worker/tests/test_worker_task_db.py`
   has the pattern), then make the timeout the visible reason without editing `pipeline/` (for example by
   raising a distinct exception class from the worker's own `asyncio.timeout` wrapper that the pipeline's
   existing handler already records by name). If the only fix is inside `pipeline/`, do not make it; put it
   under "Found elsewhere" with the exact line.
6. **Benchmark rows are not the worker's**: `run_one` refuses (log, ack, drop) a task whose `eval_run_id` is
   set. The API and sweeper never publish those; a hand-published message must not bypass the runner's manifest.
7. **The bound-equality test must not skip silently** (`worker/tests/test_worker_settings.py` uses
   `importorskip("harness.enqueue")`): make eval a dev dependency of the worker package so it always runs
   under `uv sync --all-packages`, or fail when `REPOLACE_TEST_EVAL_REQUIRED=1`. Keep the literal in
   `worker/repolace_worker/config.py` equal to `harness.enqueue.task_wall_clock_bound()`.
8. Update `.claude/rules/task-bookkeeping.md` and the "Worker concurrency and recovery" paragraph of the
   rules that describe the worker (search `.claude/rules/` for "Celery"); change the one line in `CLAUDE.md`
   that says the deployment is "owed by item 6" to say what now exists and what still needs a local run.

## Acceptance

* `docker compose -f infra/docker-compose.yml config -q` passes (or "no CLI" is stated).
* Tests for items 3 to 7 fail without the change and pass with it; all `worker` and `api` tests pass.
* No provider key, GitHub private key or token appears in `infra/*.example` except as an empty placeholder.
* The rootful socket appears nowhere except in the warning comment.

## Must not touch

`pipeline/`, `agents/`, `gateway/`, `verify/`, `eval/`, `shared/` (including the DB schema and models), the
`api/` routes and UI (package B owns those), `api/alembic/`. No migrations.
