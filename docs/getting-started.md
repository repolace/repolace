# Getting started

After this guide you will have the API and task UI running locally, a worker ready to run tasks, and know how to run a task, run the tests and read the result. Allow an hour for the first pass, mostly the GitHub App and the first image build.

This guide runs real tasks that call a model provider and spend money. The default cap is $2.00 per task. Use a toy repository first.

## Prerequisites

| Requirement | Why |
|---|---|
| Python 3.12 or newer | Every package requires it |
| [uv](https://docs.astral.sh/uv/) | Installs the workspace |
| Docker with Compose | Postgres (with pgvector), Redis, RabbitMQ, and the worker image |
| **Rootless** Docker with cgroup v2 delegation of `cpu`, `memory` and `pids` | The Verify sandbox. Without delegation the memory, CPU and process caps are silently ignored |
| git 2.38 or newer | Merge handling uses `merge-tree --write-tree`; the worker image checks this at build |
| `prlimit` (util-linux) | The agent's `grep` tool runs git under it, and the toolbox refuses to build without it |
| A GitHub App | Repository registration, issue listing, clone, push and pull requests |
| A model provider key | For `--agent llm`. See [Env files](#env-files) |

Set up rootless Docker with `systemctl --user enable --now docker` (from `docker-rootless-extras`). Check delegation with `docker info`; a missing controller shows as a warning.

Point Docker at the rootless daemon in every shell that runs compose, the CLI or the tests:

```bash
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock
```

This must be a real environment variable. A `DOCKER_HOST` line in `.env` is read into the settings object, never into `os.environ`, so the sandbox would silently use the rootful socket. Use one daemon for everything: a compose stack on the other daemon has its own Postgres.

## Install

```bash
uv sync --all-packages
```

Plain `uv sync` prunes the workspace members, because the root package declares no dependencies.

## Env files

Copy each template and fill in values. The templates hold names only.

```bash
cp .env.example .env
cp infra/api.env.example infra/api.env
cp infra/worker.env.example infra/worker.env
cp infra/site.env.example infra/site.env
```

| File | Read by | Holds |
|---|---|---|
| `.env` | Anything you run on the host: `uvicorn`, `repolace-run-task`, `repolace-eval`, `alembic`, the tests. Also compose, for variable interpolation (`infra/.env` is a symlink to it) | Database, Redis and broker URLs, GitHub App values, provider keys |
| `infra/api.env` | The `api` container | Database, Redis and broker URLs, GitHub App values, `API_BASE_URL`, `UI_ALLOWED_HOSTS`. **No provider keys** |
| `infra/worker.env` | The `worker` container | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_BASE64`, provider keys, optional `GATEWAY_STAGE_MODELS`, `WORKER_CONCURRENCY`, `WORKER_TASK_BOUND_SECONDS`, `MAX_CHANGED_FILES`, `MAX_CHANGED_LINES` |
| `infra/site.env` | The `site` container | `SITE_ALLOWED_HOSTS`, `SITE_DATABASE_URL`, optional Turnstile keys, `SITE_TRUST_CF_IP` |

The `worker-maintenance` container has no env file and needs no provider key.

Variable names that matter for a first run:

- GitHub App: `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_PRIVATE_KEY_BASE64`, `GITHUB_APP_WEBHOOK_SECRET`, `API_BASE_URL`.
- Provider keys, set only for the providers your `gateway/models.toml` routes to: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `MINIMAX_API_KEY`, and `GOOGLE_API_KEY` plus `GOOGLE_CLOUD_PROJECT` for Vertex AI (a Vertex key, not an AI Studio key).

The shipped `agent` stage routes to `claude-sonnet-5-5`. To use another priced model for one run, set `GATEWAY_STAGE_MODELS='{"agent": "<model key>"}'`. A model without a price in `gateway/models.toml` is refused before the first call.

Every variable is listed in [reference/env.md](reference/env.md).

## Start the infrastructure

From the repository root:

```bash
docker compose -f infra/docker-compose.yml up -d postgres redis rabbitmq
```

All ports bind to `127.0.0.1`: Postgres 5432, Redis 6379, RabbitMQ 5672 and its management UI 15672. Compose defaults every credential to `repolace`; set `POSTGRES_USER`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `RABBITMQ_USER` and `RABBITMQ_PASSWORD` in `.env` to change them, and keep the URLs in `.env` in step.

Apply the schema:

```bash
cd api && uv run alembic upgrade head
```

Never run `alembic upgrade` against your development database from a test run.

## Run the API

The settings require the four GitHub App values (`GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_PRIVATE_KEY_BASE64`, `GITHUB_APP_WEBHOOK_SECRET`) to exist in `.env`, so create the App (next section) first, or put placeholder values there to see the pages render.

```bash
uv run --project api uvicorn repolace_api.main:app --port 8000
```

`http://localhost:8000/ui/repos` is the task UI and `http://localhost:8000/healthz` returns `{"status": "ok"}`. The API and UI have no authentication; bind them to localhost only.

## Create the GitHub App

repolace registers repositories only through the App's installation webhook. There is no manual register route.

1. In GitHub, open Settings, Developer settings, GitHub Apps, New GitHub App.
2. **Repository permissions**: Contents read and write, Pull requests read and write, Issues read. The pipeline's preflight checks only the first two. Issues read is what listing issues needs.
3. **Subscribe to events**: Installation, and Installation repositories (the API handles `installation` and `installation_repositories`).
4. **Webhook**: URL `<public URL>/github/webhook`, and a secret of your choosing. GitHub must reach this URL, so use a tunnel for a local API (for example a Cloudflare Tunnel or ngrok). The route is exempt from the Host allowlist and authenticates by HMAC signature.
5. Generate a private key. Base64-encode the PEM onto one line and put it in `GITHUB_APP_PRIVATE_KEY_BASE64`:

   ```bash
   base64 -w0 < your-app.private-key.pem
   ```

6. Put the App id, slug and webhook secret in `.env` and `infra/api.env` (and the id and key in `infra/worker.env`), then restart the API.
7. Install the App on a test repository. Visiting `/github/install` on your API redirects to the install page.

When the installation webhook arrives, the repository appears in `/ui/repos`. If you added a tunnel hostname, add it to `UI_ALLOWED_HOSTS` or the UI answers 400 for that hostname.

Disable GitHub Actions on the test repository before the first run. The agent may edit build files such as a `Makefile`, which a push-triggered workflow would run with the repository's secrets.

## Start a worker

The worker runs tasks. Build and start both worker services:

```bash
docker compose -f infra/docker-compose.yml up -d --build worker worker-maintenance
```

Notes:

- The worker mounts the **rootless** socket at `/run/user/1000/docker.sock` by default. For another uid set `DOCKER_SOCKET` in `.env`. Never mount the rootful `/var/run/docker.sock`.
- Rebuild the image after code changes (`--build`). A stale image registers no tasks.
- Each prefork child loads the embedding model; `WORKER_CONCURRENCY` defaults to 3 (about 10 GB of RAM). Lower it on a laptop.
- Put `WORKSPACE_PARENT_DIR` (default `/var/tmp/repolace-workspaces`) on a size- and inode-limited filesystem.

More in [operations.md](operations.md).

## Run a task

### From the UI

Open `/ui/repos`, choose the repository, choose New task, pick an issue and a branch, confirm and start. The task page shows a stage strip, the diff and the test runs, and polls while the task runs.

### From JSON

```bash
curl -X POST localhost:8000/repos/<repo_id>/tasks \
  -H 'content-type: application/json' \
  -d '{"issue_number": 42, "target_branch": "main"}'
```

The response carries the task id (HTTP 201). Omit `target_branch` for the default branch. Add `"open_pr_on_failure": true` to open a PR even when the checks flag the change. A second request for an issue that already has a `queued` or `running` task on that branch returns 409.

List repository ids with `curl localhost:8000/repos`, and follow a task with `curl localhost:8000/tasks/<task_id>`.

### From the command line

The CLI runs the same `run_task` as one host process, without the queue. It claims a `queued` row, so enqueue one first (the worker would also claim it if it is running, so stop the worker or use a separate database for a pure CLI run).

```bash
uv run --all-packages repolace-run-task <task_id> [--agent llm|stub|gold] [--no-pr]
```

| Flag | Meaning |
|---|---|
| `--agent llm` | The real agent. The default |
| `--agent stub` | A deterministic editor that writes a marker comment. It proves the plumbing and says so in the PR it opens |
| `--agent gold` | Applies a benchmark instance's reference fix. Never opens a PR |
| `--no-pr` | Run everything except opening a PR |

Exit codes report whether repolace worked, not whether the issue was fixed:

| Code | Meaning |
|---|---|
| 0 | The task ran to the end, whatever the outcome |
| 1 | repolace itself broke; the task is `failed` and `error_message` says why |
| 2 | Bad invocation, task not found, or a model with no price |
| 3 | The task is not claimable (already run, or another runner has it) |

`REPOLACE_TASK_MAX_USD` replaces the $2.00 cost cap for one run. It is read from the process environment, so export it:

```bash
REPOLACE_TASK_MAX_USD=0.50 uv run --all-packages repolace-run-task <task_id> --no-pr
```

The CLI checks the model's price before claiming the task. Provider keys must be in `.env`.

### Reading the result

A task ends in one of six statuses: `queued`, `running`, `completed`, `pr_opened`, `conflicting`, `failed`. `completed` means the pipeline ran to the end without opening a PR; read `outcome` and `agent_stop_reason` for why. `failed` means repolace broke. A PR opens only when the agent submitted and no regression, silenced failure or new collection error was found, unless the task was queued with `open_pr_on_failure`.

Repositories with no pytest tests are out of scope: the baseline run produces nothing scoreable and the task completes without running the agent.

## Run the tests

```bash
uv run ruff check .
uv run --all-packages pytest -m "not db and not docker"       # no services needed
REPOLACE_TEST_DB_SUFFIX=_mybranch uv run --all-packages pytest -m db    # needs Postgres
uv run --all-packages pytest -m docker                        # needs the Docker daemon
```

- Markers: `db` needs a live PostgreSQL, `docker` needs a live Docker daemon. Both skip when the service is unreachable. Set `REPOLACE_TEST_DB_REQUIRED=1` and `REPOLACE_TEST_DOCKER_REQUIRED=1` to turn a skip into a failure, which is what you want before merging.
- Database tests truncate every table of their database. Parallel checkouts must set different `REPOLACE_TEST_DB_SUFFIX` values (an underscore, then 1 to 32 of `a-z`, `0-9`, `_`; the default is `_test`).
- Test file basenames must be unique across the repository, because there are no `__init__.py` files. This must print nothing:

  ```bash
  find . -name 'test_*.py' -not -path './.venv/*' -not -path './*/.venv/*' | xargs -n1 basename | sort | uniq -d
  ```

- After changing models, run `alembic check` from `api/`.
- The first Docker test run builds an image and needs Docker Hub access once.

## Next steps

- [Architecture](architecture.md) for what the worker does with the task
- [Operations](operations.md) for running the worker and the site
- [Benchmark](benchmark.md) to reproduce the measured run
- [Contributing](../CONTRIBUTING.md) before opening a pull request
