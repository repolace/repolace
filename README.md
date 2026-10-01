# repolace

An autonomous software-engineering agent platform. A user registers a GitHub repo, repolace fetches its open issues, the user picks one issue and a target branch, and an agent pipeline (retrieve → plan → edit → test → review) attempts a fix in an isolated sandbox and opens a PR on success.

## Status

**Phase 0–1 (active development).** The pipeline runs end to end — clone, index, baseline test run, edit, verify, score, open PR — but the edit step is a deterministic stub, not a model. Replacing it with a real agent is the remaining Phase 1 work.

| Layer | State |
|---|---|
| Monorepo scaffolding | Done — `uv` workspace, Docker Compose (Postgres + pgvector, Redis, RabbitMQ) |
| FastAPI service | Done — health check, GitHub App webhook handler, repo registration |
| GitHub App integration | Done — installation events, repo auto-registration, issue fetching |
| Database schema | Done — Alembic migrations, ORM models for installations, repos, tasks, test runs, code chunks |
| RAG layer | Done — Python AST chunking via tree-sitter, 4 embedding strategies, pgvector HNSW + full-text hybrid retrieval |
| Task pipeline | Done — task enqueue endpoint, `repolace-run-task` CLI, checkout lifecycle (one ephemeral clone per task), per-task git workspace |
| Verify sandbox | Done — rootless locked-down Docker container from a prepared per-repo image, byte-identical tree export (no `.git`), pytest report parsing, baseline + attempt scored into `tasks.outcome`, test-edit tracking |
| Git hardening | Done — credential helper scoped to host, hooks and fsmonitor disabled, global/system git config pinned to `/dev/null`, path traversal guards, symlink refusal |
| Agents (planner/editor/reviewer/debugger) | Not started — `agents/` package exists but is empty; the edit step is a stub (`pipeline/repolace_pipeline/edit.py`) |
| LiteLLM gateway | Not started — `gateway/` package exists but is empty. Phase 1, so cost is recorded from the first benchmark run |
| Retry loop | Not started — one edit attempt per task |
| Celery worker dispatch | Scaffolded — `worker/` service stands up but is unused; tasks run through the `repolace-run-task` CLI until Phase 2 |
| Eval harness | Scaffolded — `eval/harness/` exists, not yet wired to run benchmark repos |
| Observability (OTel + Grafana) | Not started — structured JSON logging with task IDs only |
| UI | Not started |

## Architecture

```
User picks issue + target branch
        │
        ▼
┌──────────────┐
│  FastAPI API  │  enqueue task
└──────┬───────┘
       │
       ▼
┌──────────────┐     ┌─────────────────┐
│  RAG layer   │────▶│  pgvector + FTS  │  retrieve relevant code
└──────┬───────┘     └─────────────────┘
       │
       ▼
┌──────────────┐
│  Plan → Edit │  single-agent loop (Phase 1)
│  → Verify    │  bounded retries on test failure
│  → Review    │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Open PR     │  squash to one commit, push, open PR
└──────────────┘
```

**Phase 1 runs synchronously, one task per process.** `POST /repos/{id}/tasks` only inserts a queued row; the pipeline runs separately through the `repolace-run-task` CLI on the host, which keeps the retrieval stack and torch out of the API image. The Celery worker and RabbitMQ containers exist from Phase 0 but are not used for pipeline execution until Phase 2, when the worker will call the same `run_task`.

## Monorepo structure

```
repolace/
  api/            FastAPI orchestrator
  worker/         Celery worker (scaffolded, not yet dispatched)
  agents/         LangGraph graph definitions (empty)
  gateway/        LiteLLM config / wrapper (empty)
  rag/            AST chunking, embedding, pgvector indexing, hybrid retrieval
  shared/         DB models, git workspace, GitHub client, common types
  pipeline/       Task lifecycle, edit orchestration, test-run parsing
  verify/         Sandbox execution, scoring, report parsing
  eval/           SWE-bench-style benchmark harness (scaffolded)
  infra/          Docker Compose
```

Each workspace package has a distinct top-level import (`repolace_api`, `repolace_shared`, `retrieval`, etc.). They previously all used `app`, which silently broke under `uv sync --all-packages`. Do not reintroduce a shared top-level name.

## Tech stack

| Component | Choice |
|---|---|
| Language | Python 3.12+ |
| API | FastAPI |
| Agent orchestration | LangGraph (planned) |
| Queue | RabbitMQ + Celery (scaffolded) |
| LLM gateway | LiteLLM (planned) |
| Vector store | PostgreSQL + pgvector |
| Embeddings | Local via `sentence-transformers` (`st-codesearch-distilroberta-base`, 768-dim) |
| Sandbox | Docker (rootless, locked-down containers) |
| Observability | Structured JSON logging (Phase 3: OpenTelemetry → Grafana) |
| Deploy | Docker Compose locally → Kubernetes later |

## Getting started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- Docker and Docker Compose, plus **rootless** Docker for the Verify sandbox (`docker-rootless-extras`, `systemctl --user enable --now docker`) with cgroup v2 delegation of `cpu`, `memory` and `pids` — without delegation the sandbox's resource caps are silently ignored
- A GitHub App (for repo registration)

### Install dependencies

```bash
uv sync --all-packages
```

### Infrastructure

```bash
cp .env.example .env
# Edit .env with your GitHub App credentials and database settings

cd infra
docker compose up -d
```

This starts Postgres (with pgvector), Redis, and RabbitMQ.

### Run migrations

```bash
cd api
uv run alembic upgrade head
```

### Start the API

```bash
uv run --project api uvicorn repolace_api.main:app --reload --port 8000
```

### Point Docker at the rootless daemon

```bash
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock
```

This must be a real environment variable. A `DOCKER_HOST` line in `.env` is read into the settings object, never into `os.environ`, so the sandbox would silently run against the rootful socket.

### Run a task

```bash
# Queue it (returns the task id)
curl -X POST localhost:8000/repos/<repo_id>/tasks \
  -H 'content-type: application/json' \
  -d '{"issue_number": 42, "target_branch": "main"}'

# Run it
uv run --all-packages repolace-run-task <task_id>
```

The task runs the repo's suite at the base commit, applies the edit, runs it again, and scores the two. A PR is opened only when the outcome is `passed`, unless the task was queued with `"open_pr_on_failure": true`. Until the real agent lands the edit is a stub, so expect that flag to be needed to see a PR.

### Run tests

```bash
uv run --all-packages pytest
```

Database-backed (`-m db`) and Docker sandbox (`-m docker`) tests skip when Postgres or the Docker daemon is unreachable. To make a missing dependency fail instead of skip, which is what you want before merging:

```bash
REPOLACE_TEST_DB_REQUIRED=1 REPOLACE_TEST_DOCKER_REQUIRED=1 uv run --all-packages pytest
```

## Index freshness

When a task starts, repolace checks the repo's `indexed_commit_sha` against the current checkout. If they differ, it runs `git diff` to find changed files and re-indexes only those — unchanged files keep their existing chunks and embeddings. Full reindex only when the previously-indexed commit is unreachable (force-push, upstream gc).

## Security model

**Verify runs inside a rootless Docker container** with:
- No network during test runs
- Dropped capabilities, `no-new-privileges`
- Read-only root filesystem, memory/CPU/PID caps
- Hard timeout

**The sandbox never receives `.git`** — only tracked source files, exported byte-identically via `git cat-file`. This prevents:
- Credential exfiltration via rewritten remotes
- Host code execution via `.git/hooks`
- Filter/attribute-based attacks from tracked `.gitattributes`

**Git runs in a sanitized environment** — `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` pinned to `/dev/null`, an allowlisted environment, hooks and fsmonitor disabled on every invocation, and a credential helper that releases the token only over `https` to the expected host. The checkout's own `.git/config` is not pinned; the sandbox never receives it, and the export reads blobs directly so no filter or attribute it names ever runs.

**Model calls run on the host, through the gateway, and the provider key lives only there.** Every call is attributed to a task, priced and recorded; an unpriced call is an error rather than zero, and a per-task budget (default $2) is enforced in the gateway rather than trusted to the agent. The key is handed to LiteLLM per call and never exported into `os.environ`, so nothing the host spawns can inherit it.

**`llm_calls` stores every request and response verbatim, which includes repository source code.** For a private repository, its code is in that table (and was sent to the model provider). Redaction strips credential-shaped strings, and the exact keys the gateway holds, from what is stored; it does not and cannot strip code. Treat the database accordingly.

## What comes next

**Phase 1 completion:** LiteLLM gateway, a real single agent in place of the stub editor, a bounded retry loop, and the benchmark (15–20 hand-picked Python issues, tracked pass/fail/cost/latency). Benchmark repos must contain no tracked symlinks or submodules — the export refuses both.

**Phase 2:** Split into specialized agents (planner/editor/reviewer/debugger), async queue dispatch, merge-conflict resolution sub-loop, basic UI.

**Phase 3:** Full OpenTelemetry instrumentation, Grafana stack, expandable benchmark.

**Phase 4:** Kubernetes, CI/CD, RBAC, failure-path polish.

## License

Not yet determined.
