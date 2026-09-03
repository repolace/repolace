# repolace

An autonomous software-engineering agent platform. A user registers a GitHub repo, repolace fetches its open issues, the user picks one issue and a target branch, and an agent pipeline (retrieve → plan → edit → test → review) attempts a fix in an isolated sandbox and opens a PR on success.

## Status

**Phase 0–1 (active development).** The foundational infrastructure and the core loop components are built; the full end-to-end pipeline is not yet wired together.

| Layer | State |
|---|---|
| Monorepo scaffolding | Done — `uv` workspace, Docker Compose (Postgres + pgvector, Redis, RabbitMQ) |
| FastAPI service | Done — health check, GitHub App webhook handler, repo registration |
| GitHub App integration | Done — installation events, repo auto-registration, issue fetching |
| Database schema | Done — Alembic migrations, ORM models for installations, repos, tasks, test runs, code chunks |
| RAG layer | Done — Python AST chunking via tree-sitter, 4 embedding strategies, pgvector HNSW + full-text hybrid retrieval |
| Task pipeline | Done — task enqueue endpoint, checkout lifecycle (one ephemeral clone per task), per-task git workspace |
| Verify sandbox | Done — rootless locked-down Docker container, byte-identical tree export (no `.git`), pytest report parsing, scoring against baseline, test-edit tracking |
| Git hardening | Done — credential helper scoped to host, `.git/config`/`.git/hooks` pinned, path traversal guards, symlink refusal |
| Agents (planner/editor/reviewer/debugger) | Not started — `agents/` package exists but is empty |
| LiteLLM gateway | Not started — `gateway/` package exists but is empty |
| Celery worker dispatch | Scaffolded — `worker/` service stands up but pipeline runs inline in the API process for now |
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

**Phase 1 runs synchronously inline in the API process** — one Docker sandbox, no queue dispatch. The Celery worker and RabbitMQ containers exist from Phase 0 but are not used for pipeline execution until Phase 2.

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
- Docker and Docker Compose
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

### Run tests

```bash
# Unit tests (no external dependencies)
uv run pytest rag/tests shared/tests pipeline/tests verify/tests

# Database-backed tests (requires running Postgres)
uv run pytest -m db

# Docker sandbox tests (requires running Docker daemon)
uv run pytest -m docker
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

**Git runs in a sanitized environment** — `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` pinned to `/dev/null`, credential helper scoped to the expected host, `.git/config` pinned to `/dev/null`.

## What comes next

**Phase 1 completion:** Wire the single-agent loop end-to-end and run the benchmark (15–20 hand-picked Python issues, tracked pass/fail/cost/latency).

**Phase 2:** Split into specialized agents (planner/editor/reviewer/debugger), async queue dispatch, merge-conflict resolution sub-loop, LiteLLM gateway, basic UI.

**Phase 3:** Full OpenTelemetry instrumentation, Grafana stack, expandable benchmark.

**Phase 4:** Kubernetes, CI/CD, RBAC, failure-path polish.

## License

Not yet determined.
