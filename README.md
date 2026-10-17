# repolace

An autonomous software-engineering agent platform. A user registers a GitHub repo, repolace fetches its open issues, the user picks one issue and a target branch, and an agent attempts a fix in an isolated sandbox, verifies it against the repo's own test suite, and opens a PR on success.

The design, with the reasoning behind each decision, is in [CLAUDE.md](CLAUDE.md) and the area files under [.claude/rules/](.claude/rules/). This file is the status and the how-to.

## Status

**Phase 0–1 (active development).** Every piece of the pipeline exists on `main`: clone, index, baseline test run, an LLM agent (graph, tools, feedback filter), verify, score, gate, open PR, and a benchmark harness. The real agent is wired into `repolace-run-task` (`--agent llm` is the default; `stub` and `gold` remain), but **no real run has happened yet**. **No benchmark number has been measured yet.**

| Layer | State |
|---|---|
| Monorepo scaffolding | Done — `uv` workspace, Docker Compose (Postgres + pgvector, Redis, RabbitMQ) |
| FastAPI service | Done — health check, GitHub App webhook handler, repo registration, issue listing, task enqueue (`POST /repos/{id}/tasks`) |
| GitHub App integration | Done — installation events, repo auto-registration, issue fetching |
| Database schema | Done — Alembic migrations 0001–0014: installations, repos, tasks, test runs, code chunks, `llm_calls`, agent outputs, the benchmark columns, task stages and heartbeat |
| RAG layer | Done — Python AST chunking via tree-sitter, 4 embedding strategies threaded through indexing and search (recorded per repo in `registered_repos.index_strategy`), pgvector HNSW + full-text hybrid retrieval |
| Task pipeline | Done — `repolace-run-task` runs `run_task` end to end (checkout lifecycle, index, baseline, agent stage, score, PR gate, squash, push, PR, terminal write). `--agent llm` (the real agent) is the default; `stub` and `gold` remain |
| Verify sandbox | Done — rootless locked-down Docker container from a prepared per-repo image, byte-identical tree export (no `.git`), pytest report parsing, scored runs plus unscored probes and scratch scripts, hidden-test overlay for benchmark instances |
| Git hardening | Done — credential helper scoped to host, hooks and fsmonitor disabled, global/system git config pinned to `/dev/null`, path traversal guards, symlink refusal |
| LiteLLM gateway | Done — stage routing, per-task budget (default $2, 150 calls, one hour), refuses an unpriced model rather than record it as free, every call recorded in `llm_calls`. The headline model is priced in `gateway/models.toml` |
| Agent | Built and wired into `repolace-run-task`, never run against a real provider — a LangGraph graph (`localize → agent → verify`, retry edge), nine tools, and a no-oracle feedback filter, in `agents/` |
| Retry loop | Built and wired in, not yet exercised by a real run — up to 3 scored attempts inside the graph |
| Benchmark harness | Built, not yet run — `eval/harness/` (`repolace-eval`): instance selection, private benchmark repos, gold validation, sweep runner, report, retrieval eval |
| Celery worker dispatch | Implemented, not yet exercised by a real run — `worker/repolace_worker/tasks.py` runs `run_task` as the Celery task `repolace.run_task`, the API publishes product tasks after commit (`api/repolace_api/publisher.py`), and `worker/repolace_worker/maintenance.py` holds the maintenance jobs. The compose `worker` service is minimal: no maintenance worker or beat, no `env_file`, no docker socket. The benchmark runner still uses the `repolace-run-task` CLI |
| Observability (OTel + Grafana) | Not started — structured JSON logging with task IDs only |
| UI | Built — server-rendered Jinja2 + HTMX under `/ui` (`api/repolace_api/ui/`): task list, new-task flow, task detail with stage strip, diff and test runs. No auth until Phase 4, so localhost or a private tunnel only. Not yet viewed against the full compose stack |

## Architecture

```
User picks issue + target branch
        │
        ▼
┌──────────────┐
│  FastAPI API  │  enqueue task (inserts a queued row)
└──────┬───────┘
       │
       ▼
┌──────────────┐     ┌─────────────────┐
│  RAG layer   │────▶│  pgvector + FTS  │  retrieve relevant code
└──────┬───────┘     └─────────────────┘
       │
       ▼
┌──────────────┐
│  Agent loop  │  one tool-using agent (Phase 1): locate, edit, probe
│  → Verify    │  scored in the sandbox; up to 3 attempts on visible failures
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Open PR     │  squash to one commit, push, open PR (gated)
└──────────────┘
```

**The pipeline runs outside the API process.** `POST /repos/{id}/tasks` inserts a queued row and publishes the task id to RabbitMQ after the commit; a Celery worker (`worker/repolace_worker/tasks.py`) claims the row and calls `run_task`. This keeps the retrieval stack and torch out of the API image. The `repolace-run-task` CLI calls the same `run_task` as one host process per task, and the benchmark runner starts several of those processes at once. The compose `worker` service is minimal today (see the status table), so the CLI remains the way to run a task end to end.

## Monorepo structure

```
repolace/
  api/            FastAPI orchestrator
  worker/         Celery worker: runs product tasks, plus maintenance jobs
  agents/         LangGraph graph, prompts, feedback filter, the nine agent tools
  gateway/        LiteLLM wrapper: routing, budget, pricing, llm_calls recorder
  rag/            AST chunking, embedding, pgvector indexing, hybrid retrieval
  shared/         DB models, git workspace, GitHub client, benchmark instance format, path guards
  pipeline/       Task lifecycle: run_task, agent seam, PR gate and text, terminal write
  verify/         Sandbox execution, scoring, report parsing, hidden-test overlay
  eval/           Benchmark harness (repolace-eval)
  infra/          Docker Compose
```

Each workspace package has a distinct top-level import (`repolace_api`, `repolace_shared`, `repolace_pipeline`, `retrieval`, `verify`, `harness`, etc.). They previously all used `app`, which silently broke under `uv sync --all-packages`. Do not reintroduce a shared top-level name.

## Tech stack

| Component | Choice |
|---|---|
| Language | Python 3.12+ |
| API | FastAPI |
| Agent orchestration | LangGraph |
| Queue | RabbitMQ + Celery |
| LLM gateway | LiteLLM |
| Vector store | PostgreSQL + pgvector |
| Embeddings | Local via `sentence-transformers` (`st-codesearch-distilroberta-base`, 768-dim) |
| Sandbox | Docker CLI (rootless, locked-down containers) |
| Observability | Structured JSON logging (Phase 3: OpenTelemetry → Grafana) |
| Deploy | Docker Compose locally → Kubernetes later |

## Getting started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- Docker and Docker Compose, plus **rootless** Docker for the Verify sandbox (`docker-rootless-extras`, `systemctl --user enable --now docker`) with cgroup v2 delegation of `cpu`, `memory` and `pids` — without delegation the sandbox's resource caps are silently ignored
- `prlimit` (util-linux) on the host. The agent's `grep` tool runs `git grep` under a memory and CPU limit, and the toolbox refuses to build without it
- A GitHub App (for repo registration, issue fetching, and opening PRs)
- To run the real agent: a provider key in `.env` (`ANTHROPIC_API_KEY`, plus `OPENAI_API_KEY`, `GEMINI_API_KEY`, `MINIMAX_API_KEY` or `GOOGLE_API_KEY` (a Vertex AI key) for comparison models), and a price for every model a stage routes to. To use Claude through Amazon Bedrock instead, set `GATEWAY_AWS_PROFILE` and `GATEWAY_AWS_REGION` (an AWS CLI profile that can invoke the model; no key) and select `claude-sonnet-5-5-bedrock`. `gateway/models.toml` routes the `agent` stage to `claude-sonnet-5-5` and prices it explicitly (LiteLLM's map does not have it); the gateway refuses an unpriced model before its first call, so a new model needs its rates (USD per million tokens) added there first
- To create the benchmark repositories: a **separate** GitHub token for the `repolace-eval fork` command only (see [Benchmark](#benchmark)). Never put it in `.env`

### Install dependencies

```bash
uv sync --all-packages
```

### Infrastructure

```bash
cp .env.example .env
# Edit .env with your GitHub App credentials, database settings and provider keys

cd infra
docker compose up -d
```

This starts Postgres (with pgvector), Redis, RabbitMQ, the API and the Celery worker.

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
uv run --all-packages repolace-run-task <task_id> [--agent llm|stub|gold] [--no-pr]
```

`--agent` chooses who makes the edit: `llm` is the real agent, `stub` is a deterministic plumbing editor that writes a marker comment (it exists to prove the pipeline end to end and says so in the PR it opens), and `gold` applies a benchmark instance's reference fix and **never opens a PR**. `--no-pr` runs everything except the PR. The exit code reports whether repolace worked, not whether the issue was fixed: `0` ran to the end (whatever the outcome), `1` repolace itself broke (the task is `failed` and `error_message` says why), `2` bad invocation or task not found, `3` task not claimable (already run, or another runner has it).

The task runs the repo's suite at the base commit, lets the agent edit and verifies each attempt, and scores the attempts against that baseline. A PR is opened only when the agent submitted and no regression, silenced failure or new collection error was found, unless the task was queued with `"open_pr_on_failure": true`. (A benchmark task opens one only when it scored `passed`.) A task that leaves no change never opens a PR. A task is `completed` when it ran to the end without opening one; read its `outcome` and `agent_stop_reason` to see why.

**The agent never runs on a repository whose baseline is unusable, and that includes a repository with no pytest tests.** If the suite at the base commit produces no scoreable result (pytest collected nothing and exited with code 5, the environment would not build, the run crashed or timed out), the task completes right after the baseline with no outcome, no agent run and no PR, and its `score_reason` starts `baseline unscoreable`. This is the benchmark's rule (a task with nothing to compare against cannot be scored) applied to product tasks as well, so issues on untested repositories are out of scope for now.

#### The agent runner

`repolace-run-task <task_id>` runs the real agent by default (`--agent llm`). It builds one gateway client per process and checks the model's price **before claiming the task**: an unpriced model exits 2 and names `gateway/models.toml`, so add the `[models.claude-sonnet-5-5.price]` entry first. Provider keys must be in `.env`. Use `--agent stub` or `--agent gold` for the deterministic runners, and `--no-pr` for a dry run. For a first smoke test use a toy repository with GitHub Actions disabled: the agent may edit build files such as a `Makefile`, which a push-triggered workflow would run.

### Run tests

```bash
uv run --all-packages pytest
```

Database-backed (`-m db`) and Docker sandbox (`-m docker`) tests skip when Postgres or the Docker daemon is unreachable. To make a missing dependency fail instead of skip, which is what you want before merging:

```bash
REPOLACE_TEST_DB_REQUIRED=1 REPOLACE_TEST_DOCKER_REQUIRED=1 uv run --all-packages pytest
```

The database tests TRUNCATE every table, so two runs against one database corrupt each other. Give each parallel checkout its own database with `REPOLACE_TEST_DB_SUFFIX=_test_<name>` (an underscore, then 1–32 of `a-z`, `0-9`, `_`). Test file names must be unique across the whole repo (pytest's import mode resolves a duplicate to whichever directory it saw first). After changing models, run `alembic check`.

## Benchmark

The claim Phase 1 is built to support: *an autonomous agent takes a GitHub issue, retrieves relevant code, patches it, verifies via the test suite and opens a PR, benchmarked at X% on N real issues, with cost and latency.* **X is not yet measured.** The harness exists and has unit tests; no gold validation, dry run or sweep has been run, so there is no number, no cost per task and no suite wall time to report. Any figure you see elsewhere is not from this repository.

What produces the number, in order (the `repolace-eval` subcommands are `select`, `fork`, `gold`, `enqueue`, `run`, `report` and `retrieval`):

```bash
# 1. Choose instances from SWE-bench Verified (network). Needs eval/harness/swebench_specs.json,
#    which is generated once; see eval/harness/specgen.py.
uv run --all-packages repolace-eval select

# 2. Create the private bench-<instance_id> repositories in the repolace org and push each
#    base commit. The token goes inline for this one command and is never put in .env.
REPOLACE_BENCH_GITHUB_TOKEN=... uv run --all-packages repolace-eval fork
#    Then install the GitHub App on those repositories; each gets a registered_repos row when
#    GitHub's `installation` webhook arrives.

# 3. Gold validation: the reference fix through the real pipeline, twice, at no LLM cost and
#    with no PRs. Instances that do not score `passed` both times are dropped, not fixed.
uv run --all-packages repolace-eval enqueue --eval-run-id gold-1 --agent gold --runs 1
uv run --all-packages repolace-eval run --eval-run-id gold-1 --agent gold
uv run --all-packages repolace-eval enqueue --eval-run-id gold-2 --agent gold --runs 1
uv run --all-packages repolace-eval run --eval-run-id gold-2 --agent gold
uv run --all-packages repolace-eval gold --runs gold-1,gold-2        # writes eval/instances/VALIDATION.md

# 4. A dry run on a few instances, one run each, to see real cost and wall time first...
uv run --all-packages repolace-eval enqueue --eval-run-id dry-1 --agent llm --model claude-sonnet-5-5 \
  --instances <id>,<id>,<id> --runs 1
uv run --all-packages repolace-eval run --eval-run-id dry-1 --agent llm --model claude-sonnet-5-5
#    ...then the sweep (3 runs per instance by default).
uv run --all-packages repolace-eval enqueue --eval-run-id sweep-1 --agent llm --model claude-sonnet-5-5
uv run --all-packages repolace-eval run --eval-run-id sweep-1 --agent llm --model claude-sonnet-5-5 \
  -k 3 --max-total-usd <cap>

# 5. The report, from the database and the run's manifest only.
uv run --all-packages repolace-eval report --run sweep-1 --gold-run gold-1 --gold-run gold-2
```

`repolace-eval retrieval` is separate: it measures whether retrieval finds the code a fix changed, per embedding strategy, with no LLM calls (`--plan` prints what it would cost first).

**How the number is computed:** passes over *planned* tasks, counting every task that is not a pass as a non-pass whatever the reason (a harness error, an unscoreable instance, a pass that edited tests). The rate over only the scoreable tasks is printed as a labelled secondary figure. The interval is over instances, because repeated runs of one instance are not independent.

**What it will and will not show.** The instances are a filtered subset of SWE-bench Verified (pure-Python pytest repositories, gold-validated, with a simple-fix filter), a public benchmark that models have probably seen. So the figure is **not the published Verified score** and does not predict performance on unseen issues, and at 15–20 instances its 95% interval is roughly 30–40 percentage points wide. Hidden fail-to-pass tests are kept from the agent's feedback by a filter, not by the sandbox, and a deliberately adversarial agent could still signal through it. `.claude/rules/benchmark.md` states each of these, with the reasoning.

## Index freshness

When a task starts, repolace checks the repo's `indexed_commit_sha` against the current checkout. If they differ, it runs `git diff` to find changed files and re-indexes only those — unchanged files keep their existing chunks and embeddings. Full reindex when the previously-indexed commit is unreachable (force-push, upstream gc), or when the embedding strategy the repo was indexed with differs from the one now configured. Two tasks on one repo wait for each other's indexing (up to 20 minutes) rather than failing.

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

**The agent's authority comes from its tools, not its prompt.** Issue text, retrieved code and test output are framed as delimited data, but the real bound is code: every path goes through one confinement function that refuses symlinks, `.git*` and (for writes) any dotfile, test or configuration file; `grep` runs resource-limited with model strings never placed before `--`; scratch scripts and test probes run in the sandbox with no network. **One known limit:** any other file is writable, including ones CI or a build executes (`Makefile`, `setup.py`). Benchmark repositories are private with Actions disabled; in product mode, do not push an agent branch to a repository whose push-triggered CI has secrets without deciding that deliberately.

**`llm_calls` stores every request and response verbatim, which includes repository source code.** For a private repository, its code is in that table (and was sent to the model provider). Redaction strips credential-shaped strings, and the exact keys the gateway holds, from what is stored; it does not and cannot strip code. Treat the database accordingly.

## What comes next

**Phase 1 completion:** gold validation, a dry run and the sweep (the headline model is already priced in `gateway/models.toml`). Benchmark repos must contain no tracked symlinks or submodules — the export refuses both.

**Phase 2:** Split into specialized agents (planner/editor/reviewer/debugger), async queue dispatch, merge-conflict resolution sub-loop, basic UI.

**Phase 3:** Full OpenTelemetry instrumentation, Grafana stack, expandable benchmark.

**Phase 4:** Kubernetes, CI/CD, RBAC, failure-path polish.

## License

Not yet determined.

### Gemini on Vertex AI

The gateway can call Gemini through Google Vertex AI with a Vertex API key. LiteLLM's own `vertex_ai/` route needs Google Cloud OAuth credentials, so `repolace_gateway/vertex.py` is a small REST transport (httpx, no extra dependency) that answers in the shape the rest of the gateway already reads.

1. Create a Vertex AI API key in the Google Cloud console (bound to a service account) and put it in `.env`: `GOOGLE_API_KEY=...`. It is read by the gateway's settings only and never exported or logged. `GOOGLE_CLOUD_PROJECT` is informational.
2. Select a model for a sweep (one model serves the whole agent for now):

   ```
   GATEWAY_STAGE_MODELS='{"agent": "vertex-gemini-3-1-pro-preview"}' uv run repolace-eval run --model vertex-gemini-3-1-pro-preview ...
   ```

| Model key | Vertex model | Used for | Price (per M tokens) |
|---|---|---|---|
| `vertex-gemini-3-1-pro-preview` | `gemini-3.1-pro-preview` | the agent (editing, debugging); the `resolve` stage follows `agent` | $2.00 in, $12.00 out, $0.20 cached |
| `vertex-gemini-3-8-flash` | `gemini-3.8-flash` | cheap steps (planning, review, summaries) once the role split lands; selectable for a whole-agent run today | $0.75 in, $3.75 out, $0.075 cached |

Neither is a default; the shipped `agent` stage is still `claude-sonnet-5-5`. A planner/editor/reviewer/debugger role table is deliberately not built yet (Phase 1 is one agent, and the role split is on `phase-2/agents`).

Prices live in `gateway/models.toml`, never in code. They were taken from Google's AI Studio pricing page, because Vertex's own page could not be read: confirm them in your Cloud console. Gemini 3.8 Flash's rates change on 2027-01-01 ($1.50 in, $7.50 out), and `gemini-3.1-pro-preview` is priced at its up-to-200k-token tier.

Retries: HTTP 429 and 5xx and timeouts are retried with exponential backoff and jitter (honouring `Retry-After`) up to `max_attempts` in `models.toml`; a 400, 401, 403 or 404 is not. Quota exhaustion (`RESOURCE_EXHAUSTED`) ends in a plain error naming it, never an endless retry.

Tokens per call (input, cached input, output including thinking) and cost are stored in `llm_calls`, and the sum in `tasks.cost_usd`. OpenTelemetry spans are Phase 3.

Live check (spends a few cents; the key is read from `.env`): `RUN_LIVE_LLM_TESTS=1 uv run pytest gateway/tests/test_gateway_vertex_live.py -s`.
