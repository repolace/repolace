# repolace — project context

This file is the persistent brief for any Claude Code session working on this repo. Read it before making architectural decisions — it captures the reasoning behind choices, not just the choices, so don't relitigate settled questions without a new reason.

## What this is

An autonomous software-engineering agent platform. A user registers a GitHub repo, repolace fetches its open issues, the user picks one issue and a target branch, and an agent pipeline (retrieve → plan → edit → test → review) attempts a fix, running in an isolated sandbox, and opens a PR on success. Previously named "Forge" — renamed to avoid collision with the well-known Minecraft modding tool and other existing projects.

**The core thesis:** most "AI coding agent" projects are common and unimpressive as a concept alone (SWE-agent, OpenHands, Aider, Devin all exist). The differentiation is in execution rigor that most hobby versions skip: a real benchmark with tracked success rate/cost/latency, production-grade reliability engineering (isolation, retries, observability), not just "it worked on my demo repo."

## Product scope (decided)

- User registers a repo (GitHub App install) → repolace indexes it (RAG) and fetches its issue list
- User picks one issue, specifies a target branch, enqueues a task
- Agent pipeline runs: retrieve relevant code → plan → edit → run tests → (loop back on failure, bounded retries) → review
- On success: PR opened against the specified branch
- **Merge conflicts:** if the target branch has moved and conflicts arise, a dedicated conflict-resolution agent (not a rerun of the Editor — reconciling two divergent intents is a distinct, harder problem) attempts a fix, looping until resolved or a retry cap is hit
- **Explicitly out of scope for now:** PR review-comment handling (agent responding to human PR feedback) — deferred
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
- Sandbox execution: Docker, via Python `docker` SDK in Phase 1–4
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
  worker/         # Celery worker + sandbox execution logic
  agents/         # LangGraph graph definitions, prompts
  gateway/        # LiteLLM config / wrapper
  rag/            # AST chunking, embedding, pgvector indexing, hybrid retrieval
  shared/         # task schemas, OTel helpers, common types
  infra/          # docker-compose.yml, k8s manifests (later)
  eval/           # SWE-bench-style benchmark harness
```

`rag/` is a separate workspace package from `shared/` (not nested inside it) so RAG-specific dependencies (tree-sitter, embedding client) don't leak into every service that imports `shared/` — same reasoning as why `gateway/` is its own package rather than living in `shared/`.

**Amendment (the one exception):** `shared/` depends on `pgvector`. The `CodeChunk` ORM model has to live in `shared/repolace_shared/db/models.py` with every other table, because Alembic autogenerate works off a single `Base.metadata` — splitting models across packages means either a second migration chain or an import graph where `shared` reaches into `rag`. `pgvector` is a thin SQLAlchemy type adapter (its only dependency is numpy), so the leak is small and bounded. The rule still holds for what actually matters: tree-sitter and sentence-transformers/torch stay in `rag/` and never reach services that only import `shared/`.

**Package naming:** every workspace package uses a distinct top-level import name (`repolace_api`, `repolace_worker`, `repolace_agents`, `repolace_gateway`, `repolace_shared`, `retrieval`, `harness`). They previously all used `app`, which silently broke both the API and the worker under `uv sync --all-packages` — editable installs append each package root to `sys.path`, so four `app/` directories shadowed each other in .pth order. Do not reintroduce a shared top-level name.

**Note:** `uv sync` alone prunes workspace members, because the root package declares no dependencies. Use `uv sync --all-packages`.

## Logging / observability timing (decided explicitly — don't relitigate)

- **Phase 1**: structured JSON logging with a task ID on every log line, from day one. Cheap now, expensive to retrofit across every agent call later.
- **Phase 3**: full OpenTelemetry instrumentation (one task = one trace, each pipeline stage = a span with timing/cost/status) + Grafana stack. Deliberately *not* done in Phase 1 — at MVP scale (synchronous, one sandbox, no queue) there's no concurrent complexity yet for a trace to meaningfully stitch together. Tracing earns its cost once Phase 2's async queue/workers introduce real concurrency.

## Build phases

**Phase 0 — Scaffolding (~15–20h)**
Monorepo setup, Docker Compose (Postgres + Redis + RabbitMQ), FastAPI skeleton, GitHub App install for repo registration + issue fetching. No agent logic yet. The `worker` service (Celery, broker=RabbitMQ, result backend=Redis) is also stood up in Phase 0 — ahead of when Phase 1 actually needs it, since Phase 1's pipeline runs synchronously inline in the API process (see below); the worker container sits unused until Phase 2 wires real task dispatch through it.

**Phase 1 — MVP: single-agent loop (~100–120h) — the resume-worthy checkpoint**
- One repo, repo-aware RAG (pgvector)
- Single agent (not yet split by role) doing plan → edit → run tests → retry on failure, via LangGraph, bounded retries
- Synchronous execution, inline in the API process — one Docker sandbox, no queue dispatch yet (the Phase 0 worker/RabbitMQ/Redis containers exist but aren't used for pipeline execution until Phase 2)
- Opens a real PR on success
- Structured JSON logging with task IDs (see above)
- Benchmark: 15–20 hand-picked real GitHub issues, tracked pass/fail, cost, latency
- Target claim: "autonomous agent takes a GitHub issue, retrieves relevant code, patches it, verifies via test suite, opens a PR — benchmarked at X% on N real issues"

**Phase 2 — Real product (~60–80h)**
- Split into specialized agents (planner/editor/reviewer/debugger)
- Async queue (RabbitMQ/Celery), worker pool
- Merge-conflict resolution sub-loop (own agent role, bounded retries)
- LiteLLM gateway wired in
- UI: task list with status badges, enqueue flow (issue → branch → confirm), pipeline view (log-backed, not full traces yet)

**Phase 3 — Observability and testing rigor (~55–70h)**
- Full OpenTelemetry instrumentation + Grafana stack (Tempo/Prometheus/Loki)
- Pipeline-trace UI switches from log-based to real spans
- Full test suite: unit, integration, E2E through the whole pipeline
- Expand and track the benchmark over time

**Phase 4 — Deployment / hardening (~40–50h)**
- Kubernetes + CI/CD (GitHub Actions)
- RBAC scoped to task submission + dashboard access only (not PR approval)
- Failure-path polish: what a user sees on failure, retry/resume semantics if a worker dies mid-task

**Phase 5 — Replace prebuilt pieces (open-ended)**
See replacement order above.

**Rough totals:** Phase 0–1 ≈ 115–140h → 8–15 weeks at 9–15h/week. Phases 2–4 add ≈155–200h more. Phase 5 is ongoing.

## UI (mocked, not yet built)

- Task list per repo: rows with git-flavored status badges (`queued`/`running`/`merged`/`conflicting`/`failed`) — keep this vocabulary consistent with the DB task-state enum
- Enqueue flow: pick issue → pick target branch → confirm screen (shows PR target branch + a real computed cost estimate, not a static placeholder) → start
- Pipeline trace view: horizontal stage strip (Retrieve/Plan/Edit/Test/Review) with per-stage status color and duration — in Phase 2 this is backed by logs; in Phase 3 it's backed by real OTel spans

## Open questions / not yet decided

- Exact "task completed successfully" definition (existing tests only, or does the agent add new tests for the fix? diff-scope constraints?) — needs to be pinned down before the benchmark can be meaningful
- Issue-list filtering rule (label-based, e.g. only `bug`/`good-first-issue`) so users aren't picking from unfiltered noise
- Single-flight vs. concurrent tasks per repo (affects whether Phase 1 needs to worry about two agents touching the same repo state)
- **Embedding truncation.** The chosen model's `max_seq_length` is **128 tokens**. Indexing this repo produced 191 chunks, of which **50 (26%) exceeded that budget** and were silently truncated — they are embedded from their opening lines alone. This blunts the class-skeleton chunks specifically, since those are the longest. Options: a model with a longer context, splitting oversized chunks, or embedding a signature/docstring summary instead of the full body. Needs deciding before the benchmark, since it directly caps retrieval quality.
- **Unauthenticated `/github/callback`.** It takes `installation_id` from a query param with no `state` or signature check, then mints a token and writes to the DB. Because `_upsert_repos` sets `is_active=True` unconditionally, this can re-activate repos that a `suspend`/`deleted` webhook deactivated. RBAC is deferred to Phase 4, but this specific endpoint is a write path, not just a read, so it may deserve fixing sooner.
- Docker sandbox isolation mechanism (Docker-in-Docker, host socket mount, remote Docker daemon, or a stronger isolation layer like gVisor/Kata/Firecracker) — "isolated Docker sandboxes" is called mandatory in Architecture above, but the mechanism itself isn't picked yet. Matters because the naive shortcut (mounting the host's `/var/run/docker.sock` into the worker) lets sandboxed LLM-generated code escape to the host, defeating the isolation goal entirely. Decide when actually building the Verify stage's sandbox runner, not before.
