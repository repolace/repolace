# repolace — project context

This file is the persistent brief for any Claude Code session working on this repo. Read it before making architectural decisions — it captures the reasoning behind choices, not just the choices, so don't relitigate settled questions without a new reason.

## What this is

An autonomous software-engineering agent platform. A user registers a GitHub repo, repolace fetches its open issues, the user picks one issue and a target branch, and an agent pipeline (retrieve → plan → edit → test → review) attempts a fix, running in an isolated sandbox, and opens a PR on success. Previously named "Forge" — renamed to avoid collision with the well-known Minecraft modding tool and other existing projects.

**The core thesis:** most "AI coding agent" projects are common and unimpressive as a concept alone (SWE-agent, OpenHands, Aider, Devin all exist). The differentiation is in execution rigor that most hobby versions skip: a real benchmark with tracked success rate/cost/latency, production-grade reliability engineering (isolation, retries, observability), not just "it worked on my demo repo."

## Area rules

Area-specific design lives in `.claude/rules/`, one file per area, each loaded by Claude Code when it reads or edits a file matching the file's `paths:`. They hold the reasoning behind the "decided" choices; read the matching file before changing that area. Section cross-references elsewhere in this file point into these files.

- `.claude/rules/git-and-pr-flow.md`: Branch and PR flow within a task (paths: `shared/repolace_shared/git/**`, `pipeline/**`, `agents/repolace_agents/resolver.py`)
- `.claude/rules/index-and-checkout.md`: Index freshness, checkout lifecycle, concurrency (paths: `rag/**`, `shared/repolace_shared/git/workspace.py`, `pipeline/**`)
- `.claude/rules/verify-sandbox.md`: Verify sandbox: containment, .git as configuration, byte-identical export (paths: `verify/**`, `shared/repolace_shared/paths.py`, `shared/repolace_shared/git/**`)
- `.claude/rules/verify-agent-extensions.md`: Verify extensions for an agent: run kinds, overlay, argv, report integrity (paths: `verify/**`, `agents/repolace_agents/tools/**`, `pipeline/**`)
- `.claude/rules/success-criteria.md`: Task success criteria: fail-to-pass, pass-to-pass, outcomes (paths: `verify/**`, `pipeline/**`, `api/alembic/**`, `eval/**`)
- `.claude/rules/agent.md`: The agent: graph, bounds, nine tools, stop reasons, conflict resolver (paths: `agents/**`, `gateway/**`, `pipeline/**`)
- `.claude/rules/no-oracle.md`: The no-oracle protocol: hidden tests never reach the agent (paths: `agents/**`, `verify/**`, `pipeline/**`, `eval/**`)
- `.claude/rules/prompt-injection.md`: Prompt-injection posture (paths: `agents/**`, `pipeline/**`)
- `.claude/rules/benchmark.md`: The benchmark: instances, bench repos, sweep, report (paths: `eval/**`, `shared/repolace_shared/instances.py`, `verify/specs.toml`)
- `.claude/rules/task-bookkeeping.md`: Task bookkeeping: migrations 0012-0014, stages, heartbeat (paths: `shared/repolace_shared/db/**`, `api/alembic/**`, `pipeline/**`, `worker/**`)
- `.claude/rules/api-and-ui.md`: API and the server-rendered UI (paths: `api/**`, `ui/**`)
- `.claude/rules/later-visions.md`: Later product visions: PR conversation handling, live workspace view (paths: `api/**`, `ui/**`, `worker/**`)
- `.claude/rules/open-questions.md`: Open questions and not yet decided (paths: `rag/**`, `eval/**`, `api/**`)

## Product scope (decided)

- User registers a repo (GitHub App install) → repolace indexes it (RAG) and fetches its issue list
- User picks one issue, specifies a target branch, enqueues a task
- Agent pipeline runs: retrieve relevant code → plan → edit → run tests → (loop back on failure, bounded retries) → review
- On success: PR opened against the specified branch
- **Merge conflicts:** if the target branch has moved and conflicts arise, a dedicated conflict-resolution agent (not a rerun of the Editor — reconciling two divergent intents is a distinct, harder problem) attempts a fix, looping until resolved or a retry cap is hit
- **Explicitly not before the MVP works:** PR conversation handling and the live workspace view. Both are wanted, both are recorded with their reasoning under "Later product visions", and neither is started until the core loop is verified and benchmarked.
- **Explicitly NOT repolace's job:** who can approve/merge a PR — that's GitHub's branch protection / required reviewers, not rebuilt here. repolace's own RBAC is scoped only to who can submit tasks and see cost/trace data.

## Architecture

Four-stage core loop, the hard/interesting part of the whole system:
1. **Retrieve** — repo-aware RAG (chunk repo into functions/classes, embed, retrieve relevant chunks for the issue) — necessary because full repos don't fit in context
2. **Plan** — Planner agent produces a change plan from the issue + retrieved code
3. **Edit** — Editor agent produces the actual diff
4. **Verify** — run the test suite in an isolated sandbox; on failure, a Debugger agent gets the failure output and revises, looping back to Edit (bounded retries)
5. **Review** — Reviewer agent sanity-checks the diff against the plan before PR

Agents are split by role (planner/editor/reviewer/debugger/conflict-resolver) rather than one large prompt, because a single agent conflating "what to do" with "doing it" gets unreliable with full repo + plan + edit context all at once. Splitting was deferred to Phase 2, after feeling where the Phase 1 single-agent version actually overloads.

**Why distributed-systems pieces are justified here** (not decoration — asked and answered explicitly during planning):
- **Isolated Docker sandboxes**: mandatory, not optional — this runs LLM-generated code, needs isolation regardless of scale, true even at N=1 concurrent task
- **Async job queue**: justified by task *duration* (a task can run minutes: clone, retrieve, plan, edit, test, retry), not by volume — needed so requests aren't held open and crashed workers don't lose in-flight tasks
- **LLM gateway**: real production concern (routing cheap vs. strong models per step, cost tracking) — not just an abstraction

## Tech stack

**Phase 1–4 (prebuilt tools; deliberate choice — de-risk product logic first, replace pieces later once real requirements are known from usage):**
- Orchestrator/API: Python, FastAPI
- Agent orchestration: LangGraph (already used at Accenture — planner/editor/reviewer/debugger map onto graph nodes with conditional edges for retry/conflict loops)
- Queue: RabbitMQ + Celery
- LLM gateway: LiteLLM (model routing + cost tracking)
- Vector store: pgvector (Postgres extension — repo-sized corpora don't need a dedicated vector DB; revisit only if deliberately demoing sharding under load)
- Embeddings: **local**, via `sentence-transformers` (`flax-sentence-embeddings/st-codesearch-distilroberta-base`, 768-dim) — deliberately *not* routed through the LLM gateway. Indexing embeds every chunk in a repo, so a per-call API price would make reindexing the dominant cost of the whole system and make benchmark runs expensive to repeat. A local code-tuned model is free, deterministic across runs (which the benchmark needs), and has no rate limit. Cost: it pulls torch (~2–3 GB) into any image that installs `rag/`. Revisit if the image size becomes the binding constraint, or if retrieval quality plateaus below what a hosted embedding model would give.
- DB: PostgreSQL (task/job/run state, issue metadata, cost/trace summaries)
- Sandbox execution: Docker, driven through the `docker` **CLI** (`verify/verify/backends/docker.py`), not the Python SDK: the argv *is* the security policy, so it is worth being a value the code can print, diff and assert on flag by flag rather than keyword arguments spread across an API call
- Observability: OpenTelemetry SDK → Grafana Tempo (traces) + Prometheus (metrics) + Loki (logs)
- Deploy: Docker Compose locally → Kubernetes (k3s locally, real cluster if needed) once the loop is stable; CI/CD via GitHub Actions

**Phase 5 replacement candidates** (highest payoff first, only after Phase 1–4 usage reveals real requirements):
1. Sandbox execution → custom Go implementation (isolation/lifecycle management fits existing systems-programming strength — see prior projects: a recursive DNS resolver, a BitTorrent peer-wire client, a bytecode VM)
2. LLM gateway → custom (once real routing/cost needs are known)
3. Queue → custom Redis-based (lower priority — Celery/RabbitMQ genuinely work well here; mainly a "prove you can" line, not a product improvement)
4. LangGraph → probably skip or last; least likely to actually beat the prebuilt tool for this fixed set of agent roles

**Repo structure (monorepo — justified: solo dev, tightly coupled services during active development, no reason to pay multirepo coordination overhead):**
```
repolace/
  api/            # FastAPI orchestrator
  worker/         # Celery worker: runs product tasks via `repolace.run_task` (tasks.py), plus maintenance roles
  agents/         # LangGraph graph, prompts, feedback filter, the nine tools
  gateway/        # LiteLLM wrapper: routing, budget, pricing, llm_calls recorder
  rag/            # AST chunking, embedding, pgvector indexing, hybrid retrieval
  shared/         # DB models, git workspace, GitHub client, instance format, path guards
  pipeline/       # run_task: the stages around the agent, gate, PR text, finalize
  verify/         # Sandbox backend, scoring, report parsing, hidden-test overlay
  infra/          # docker-compose.yml, k8s manifests (later)
  eval/           # SWE-bench-style benchmark harness (repolace-eval)
```

Dependency direction, which two review rounds enforced: `shared` ← `verify` ← `agents`, `shared` ← `gateway` ← `agents`, and `pipeline` → {`agents`, `gateway`, `rag`, `verify`, `shared`}. **`agents` must not import `rag`** (that is what keeps torch out of agent tests; `agents/pyproject.toml` does not list it). `agents` does import `verify.scoring`, which loads SQLAlchemy and pgvector through `shared`'s models, but not torch or LiteLLM.

**Indexing is Python-only for Phase 1 — deliberate, with multi-language support planned later.** The chunker uses `tree-sitter-python` and Python node types (`function_definition`, `class_definition`, `decorated_definition`), and `find_python_files` matches `*.py`. A repo in any other language indexes to zero chunks and retrieval returns nothing. Two consequences to hold onto:

- **The Phase 1 benchmark repos must be Python.** A non-Python issue in the 15–20 hand-picked set would score as a failure for reasons that have nothing to do with the agent.
- The empty-index case is logged (`rag.index.no_python_files`) rather than passing silently, since zero chunks otherwise looks exactly like a successful index.

Extending is a contained change, not a rewrite: add the grammar package, and map that language's node types onto the same function/method/class-skeleton/module shape the chunker already emits. The storage, embedding, retrieval and RRF layers are language-agnostic already.

`rag/` is a separate workspace package from `shared/` (not nested inside it) so RAG-specific dependencies (tree-sitter, embedding client) don't leak into every service that imports `shared/` — same reasoning as why `gateway/` is its own package rather than living in `shared/`.

**Amendment (the one exception):** `shared/` depends on `pgvector`. The `CodeChunk` ORM model has to live in `shared/repolace_shared/db/models.py` with every other table, because Alembic autogenerate works off a single `Base.metadata` — splitting models across packages means either a second migration chain or an import graph where `shared` reaches into `rag`. `pgvector` is a thin SQLAlchemy type adapter (its only dependency is numpy), so the leak is small and bounded. The rule still holds for what actually matters: tree-sitter and sentence-transformers/torch stay in `rag/` and never reach services that only import `shared/`.

**Package naming:** every workspace package uses a distinct top-level import name (`repolace_api`, `repolace_worker`, `repolace_agents`, `repolace_gateway`, `repolace_shared`, `repolace_pipeline`, `retrieval`, `verify`, `harness`). They previously all used `app`, which silently broke both the API and the worker under `uv sync --all-packages` — editable installs append each package root to `sys.path`, so four `app/` directories shadowed each other in .pth order. Do not reintroduce a shared top-level name.

**Note:** `uv sync` alone prunes workspace members, because the root package declares no dependencies. Use `uv sync --all-packages`.

## Logging / observability timing (decided explicitly — don't relitigate)

- **Phase 1**: structured JSON logging with a task ID on every log line, from day one. Cheap now, expensive to retrofit across every agent call later.
- **Phase 3**: full OpenTelemetry instrumentation (one task = one trace, each pipeline stage = a span with timing/cost/status) + Grafana stack. Deliberately *not* done in Phase 1 — at MVP scale (synchronous, one sandbox, no queue) there's no concurrent complexity yet for a trace to meaningfully stitch together. Tracing earns its cost once Phase 2's async queue/workers introduce real concurrency.

## Build phases

**Phase 0 — Scaffolding (~15–20h)**
Monorepo setup, Docker Compose (Postgres + Redis + RabbitMQ), FastAPI skeleton, GitHub App install for repo registration + issue fetching. No agent logic yet. The `worker` service (Celery, broker=RabbitMQ; task results are ignored, there is no result backend) is also stood up in Phase 0 — ahead of when Phase 1 actually needs it, since Phase 1's pipeline runs as a host process per task, not through a queue (see below); the worker sat unused until Phase 2 wired real task dispatch through it (see "What exists now" below).

**Phase 1 — MVP: single-agent loop (~100–120h) — the resume-worthy checkpoint**
- One repo, repo-aware RAG (pgvector)
- Single agent (not yet split by role) doing plan → edit → run tests → retry on failure, via LangGraph, bounded retries
- **The LiteLLM gateway goes in from the start, not in Phase 2** (moved deliberately — do not move it back). Every model call is routed through `gateway/` on the agent's first run, so cost per task is recorded from the first benchmark run rather than retrofitted. The benchmark's headline is "success rate, cost and latency"; cost that starts being measured halfway through the instance set is not comparable across the set, and the earlier runs cannot be re-priced without re-running them — the same argument as `task_test_runs` storing raw sets. The cost of pulling it forward is an abstraction over one provider before the routing requirements are known, which is small and reversible; the cost of deferring it is an unrepeatable measurement, which is not.
- Verify: the repo's suite runs in a rootless locked-down container from a prepared per-repo image, against a source tree exported without `.git`, once at the base commit and once after the edit — and the two runs are scored into `tasks.outcome` (see "Verify sandbox"). Pulled forward from Phase 2, where this bullet used to live: the success criteria are unfalsifiable without a baseline, so a Phase 1 that opens PRs it cannot score has no benchmark to report.
- **One host process per task, no queue.** `POST /repos/{id}/tasks` only inserts a `queued` row; `repolace-run-task <task_id>` (`pipeline/repolace_pipeline/cli.py`) claims it and runs `run_task` to completion in its own process, on the host, against the containerised Postgres. (The CLI process, not the API process, runs the task: that is what keeps torch and the retrieval stack out of the API image.) The benchmark runner starts K such processes at a time, which is concurrency without a queue. That is still how benchmark rows run; product tasks the API publishes run in the Celery worker instead (see "What exists now" below).
  **What Phase 2 changes:** a Celery worker calls the same `run_task` with a long-lived engine, GitHub client and sandbox backend instead of the CLI's per-process ones (that is why `run_task` owns none of them), the process-per-task isolation goes away, and the claim guard (`queued` → `running` is conditional) becomes load-bearing because a queue delivers at-least-once.
  **What exists now (Phase 2, item 2):** `worker/repolace_worker/tasks.py` defines `repolace.run_task` (`acks_late`, `reject_on_worker_lost`), which runs `run_task` on one persistent asyncio loop per prefork child. `Resources` (engine, GitHub client, sandbox backend, specs, gateway client with its preflight of the `agent` and `resolve` stages) is built once per child by `pipeline/repolace_pipeline/resources.py::build_resources`, the same function the CLI calls, and the embedder is warmed per child, never in the prefork parent. `claimed_by` is `<hostname>:<child pid>`. The task is wrapped in `asyncio.timeout(WorkerSettings.task_bound_seconds)` (default 11,700 s, the literal of `harness.enqueue.task_wall_clock_bound()`, held equal by a test when eval is importable); Celery's hard `task_time_limit` is that plus 600 s. `TaskNotClaimable` and `TaskNotFound` are logged and the message acked and dropped; any other exception is logged and not retried (a row is never re-run). The API publishes by name after the commit (`api/repolace_api/publisher.py`); a publish failure is logged and never fails the request. The CLI remains for the benchmark runner, which owns `eval_run_id` rows.
- Opens a real PR on success
- Structured JSON logging with task IDs (see above)
- Benchmark: 15–20 hand-picked real GitHub issues, tracked pass/fail, cost, latency
- Target claim: "autonomous agent takes a GitHub issue, retrieves relevant code, patches it, verifies via test suite, opens a PR — benchmarked at X% on N real issues"

**Phase 2 — Real product (~60–80h)**
- Split into specialized agents (planner/editor/reviewer/debugger)
- Async queue (RabbitMQ/Celery), worker pool
- Merge-conflict resolution sub-loop (own agent role, bounded retries): wired, as the `merge_target` stage (`pipeline/repolace_pipeline/conflicts.py`; product tasks whose gate would open a PR, never benchmark tasks)
- UI: task list with status badges, enqueue flow (issue → branch → confirm), pipeline view (log-backed, not full traces yet)

**Phase 3 — Observability and testing rigor (~55–70h)**
- Full OpenTelemetry instrumentation + Grafana stack (Tempo/Prometheus/Loki)
- Pipeline-trace UI switches from log-based to real spans
- Full test suite: unit, integration, E2E through the whole pipeline. **Database-backed tests specifically**: the one defect that reached a real run (a `MissingGreenlet` from reading an ORM attribute after `rollback()` expired it) was in a DB path with no coverage, while 148 passing tests touched no database
- Expand and track the benchmark over time

**Phase 4 — Deployment / hardening (~40–50h)**
- Kubernetes + CI/CD (GitHub Actions)
- RBAC scoped to task submission + dashboard access only (not PR approval)
- Failure-path polish: what a user sees on failure, retry/resume semantics if a worker dies mid-task

**Phase 5 — Replace prebuilt pieces (open-ended)**
See replacement order above.

**Rough totals:** Phase 0–1 ≈ 115–140h → 8–15 weeks at 9–15h/week. Phases 2–4 add ≈155–200h more. Phase 5 is ongoing.

## Deployment requirements for the sandbox host

- **One Docker daemon for everything, and it must be the rootless one.** A machine can run a rootful system daemon and a rootless user daemon side by side, and plain `docker` talks to the rootful one unless `DOCKER_HOST` says otherwise. Run the compose stack, the CLI and the runner with `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`: a stack on the other daemon has its own Postgres, so the API registers repositories in a database the CLI never reads.
- **Rootless Docker with cgroup v2 delegation of `cpu`, `memory` and `pids`** (see "Rootless is a deployment precondition" in `.claude/rules/verify-sandbox.md`); without it the resource caps are silently ignored.
- **The workspace's parent directory must be on a size- and inode-limited filesystem.** A probe or script can write as much as it likes into its own export, a bind mount that no container limit covers, and `discard` can leave behind files owned by the sandbox's subuid that the host cannot delete. `task_workspace` takes a `parent_dir`, and the CLI passes `PipelineSettings.workspace_parent_dir` (env `WORKSPACE_PARENT_DIR`) to it; **left unset, the temp directories are created under the process's `TMPDIR`**, so point one of the two at the quota'd filesystem.
- **`prlimit` (util-linux) on the host.** The `grep` tool runs git under it, and `build_toolbox` calls `require_prlimit()` so a missing binary fails when the toolbox is built, not on the first search a model makes.
- **Expect an image rebuild the first time a run follows an upgrade.** The plugin's bytes are part of the image cache key (it is `COPY`ed in), and the plugin changed when the per-run nonce was added.
- **A provider key.** `ANTHROPIC_API_KEY` must be in `.env` (the key is handed to LiteLLM per call and is never exported into `os.environ`). For the Vertex headline run it is `GOOGLE_API_KEY` (a Vertex AI key, not an AI Studio one). Every model's price is in `gateway/models.toml`.
- **The worker deployment (item 6, partly done).** `worker/Dockerfile` and a `worker` service in `infra/docker-compose.yml` exist but are minimal: the service runs one default-queue worker with no `-Q`, and it has no maintenance worker, no `celery beat`, no `env_file`, no docker socket and no `infra/rabbitmq.conf`, so it cannot yet run a task end to end. What a full deployment still needs is two worker processes and a beat: the task worker (default queue, concurrency from `WORKER_CONCURRENCY`), a maintenance worker (`WORKER_ROLE=maintenance`, `-Q repolace.maintenance`, concurrency 1) that needs the **docker CLI and socket** (the reaper removes containers), and `celery beat`. The task worker's image needs git, `prlimit`, the docker CLI and socket (rootless daemon), `env_file` with the App and provider credentials, `repolace-pipeline` installed, and `WORKSPACE_PARENT_DIR` on a size- and inode-limited filesystem. RabbitMQ's `consumer_timeout` must exceed `task_time_limit` (11,700 + 600 s) or it kills `acks_late` tasks; set it in `infra/rabbitmq.conf`. The API needs `CELERY_BROKER_URL`.

## Operations

- `uv sync --all-packages` (plain `uv sync` prunes workspace members).
- **Test database isolation:** `db_session` TRUNCATEs every table before every test, so two test runs that share a database corrupt each other with failures that look like real bugs. Parallel checkouts each set `REPOLACE_TEST_DB_SUFFIX` (an underscore, then 1-32 of `a-z`, `0-9`, `_`; default `_test`; an empty value is refused). `REPOLACE_TEST_DB_REQUIRED=1` and `REPOLACE_TEST_DOCKER_REQUIRED=1` turn a missing Postgres or daemon from a skip into a failure.
- **Test file basenames must be unique across the whole repository** (there are no `__init__.py` files, so pytest's prepend import mode resolves `test_config.py` to whichever directory was collected first, and a duplicate fails only in a whole-suite run). Prefix new files with their area; check with `find . -name 'test_*.py' -not -path './.venv/*' -not -path './*/.venv/*' | xargs -n1 basename | sort | uniq -d`, which must print nothing. The same holds for `*_support.py` helpers.
- **Lint with `uv run ruff check .`.** The rule set is the root `pyproject.toml`'s `[tool.ruff.lint]`, each ignore commented. A `# noqa` carries its reason after ` -- `; check for stale ones with `--extend-select RUF100`. There is no formatter gate.
- **The docker tests need the registry for their first image build.** A build resolves the base image's metadata from Docker Hub, and a timed-out request there once failed the overlay test in a stress run. The overlay test now pulls the base image once per session with three tries and skips, with the reason, if the registry is unreachable.
- **Run `alembic check` after touching models.** It is what caught the missing GIN and HNSW index declarations.
- No `alembic upgrade` against the development database from a test run: the `-m db` fixtures migrate their own `<db>_test<suffix>` database.

## Phase 1 status

**On `main`:** the sandbox and scoring extensions, the nine tools, retrieval strategy plumbing and `build_query`, the agent graph with its feedback filter, the pipeline seams, gate, PR text and finalize, the benchmark harness, and the integration that wires the real agent into `repolace-run-task`. **Not yet measured:** any benchmark number, any retrieval number, the cost per task, the suite wall times, whether the curated fail-to-pass ids match what our plugin reports, and any real run against a provider. The 32 Docker containment tests have passed on the rootless daemon. **What produces the number:** gold validation, a dry run, the sweep, then `repolace-eval report`.

**Running it:** `docs/PHASE1_TESTING.md` is the local order of operations (preflight, smoke test, dry run, sweep, report), with `scripts/phase1_preflight.sh` as its read-only check. Code work that does not need Docker or secrets is split into four self-contained packages in `docs/cloud-work/` for cloud sessions.

**The headline model for the first run is Gemini 3.1 Pro on Vertex AI** (`vertex-gemini-3-1-pro-preview`, selected per sweep with `--model`; the shipped `agent` stage default is still `claude-sonnet-5-5`, which becomes a comparison column when its access works). Reason: it is the provider with a working key; Bedrock was blocked by account verification. The live Vertex test passed (both models answer, and a tool-call round trip with the packed thought signature is accepted); the model prices were taken from Google's AI Studio page, not Vertex's own, and are to be confirmed in the Cloud console. Gemini 3.8 Flash's rates change on 2027-01-01.

**How the real agent is wired (merged; verified by reading the code and by the integration stream's `-m db` tests, not by a real run):**

1. `pipeline/repolace_pipeline/agent_runner.py::LLMAgent` calls `run_agent` (never `run_graph`, so every run draws a fresh nonce) with the `AgentDeps` the pipeline builds, inside the gateway `task_scope` and `TaskBudget` the agent stage opens; the budget clock starts at the agent stage, not before the clone, index and baseline.
2. `cli.py` builds one gateway client per process and checks the agent stage's model (and fallback) **before any task row is claimed**: a model with no price is exit code 2 naming `gateway/models.toml`. Provider keys must be in `.env` (the runner's environment allowlist does not pass them to children). `GATEWAY_STAGE_MODELS` is honoured through the gateway's own loader.
3. **The CLI default is `--agent llm`**; `stub` and `gold` remain, `gold` forces no PR. Exit codes: 1 only when `error_message` is set; 2 usage, not found or unpriced model; 3 not claimable.
4. Retrieval uses `build_query(issue_title, issue_body)`: the semantic arm gets `query.semantic`, the keyword arm `query.keyword`, and the strategy is the repo's stored `index_strategy` (else `truncate`). A title made only of control characters falls back to `issue <N>` rather than failing retrieval.
5. Benchmark mode passes `expected_fail_to_pass` from the loaded instance into `score(...)`, so a PASSED reason no longer ends "uncurated".
6. **`GraphRecursionError` is a harness error (FAILED), decided.** The graph's shape (at most 7 steps against a limit of `4 * max_attempts + 8`) cannot reach that limit by itself and nothing in an issue or repository can cause it, so only a routing bug in repolace can. Counting it as a step cap would hide the bug inside the headline denominator, and a run that had already scored a passing attempt could even open a PR. `_agent_stop_from` maps only `BudgetExceeded` and `LLMCallError`.
7. **Known limits recorded by the integration security review:** an authentication or billing failure from the provider **used to be** scored as an agent failure (`llm_error`); it is **now a harness failure**: `LLMCallError.kind` is `auth` or `quota`, the graph and resolver re-raise it, the task ends `failed` with a fixed prefix in `error_message`, and the eval runner stops dispatching after two such failures *in one invocation*. A rate limit that outlasts the bounded retries is classed `quota` too, so a recoverable throttle can cost a task; any non-protected file is writable, including files CI executes (`Makefile`, `noxfile.py`), so a pushed agent branch can carry an edited build file: with `llm` now the default, run the first smoke test on a toy repository with GitHub Actions disabled; the retrieval index is per repository, not per commit, so concurrent product-mode tasks on one repository can see each other's index.

## Unverified

Claims in this file that rest on reading, not on running: everything about real GitHub behaviour (token scopes, the `workflow` scope, organisation permissions, `PUT /user/installations/...`, the 422 on an existing repository, disabling Actions); anything about a real provider; that the sibling report's host path is discoverable from inside a container; and every figure about cost, wall time and memory per task, which come from the plan's estimates or the source's comments, not measurements.

