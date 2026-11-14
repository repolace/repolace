> **Archived, personal interview notes.** Written before the worker, the public site, the AWS deployment and billing existed. It is kept for history, not maintained, and is not part of the public documentation.

# repolace: the interview guide

Written 2026-10-19 from a read of the repository at `main` (`10c4293`). **Updated 2026-10-25** (checked against `main` at
`f02ccb3` plus uncommitted work, and the development database). The update adds the first Phase 1 benchmark results
(section 13b), corrects the "no headline number" statements, records the gateway changes for two new providers
(sections 9, 15, 16), the role-split experiment, the branch clean-up (section 17), four new interview questions
(section 18) and operating notes (section 20). Sections 1 to 12 and 14 were not re-read; they still describe the code
as read on 2026-10-19. Purpose: be able to explain every part of this project, say where the code lives, say how it
works, and defend the decisions, the way you would in an interview about something you built.

**How much of this I verified, so you know what to trust.**

- **Read in full (code):** `pipeline/run.py`, `attempts.py`, `gate.py`, `runners.py`, `cli.py`, `resources.py`;
  `agents/graph.py`, `contracts.py`, `state.py`, `prompts.py`, `tools/base.py`, `tools/toolbox.py`, `EditFile`;
  `verify/scoring.py`, `protocol.py`, `stage.py`, the argv builders in `backends/docker.py`, the pytest plugin,
  the first half of `report.py`; `gateway/client.py`, `budget.py`; `rag/` (chunker, index, query, retrieve,
  ranking, embed); `shared/db/models.py`, `git/workspace.py`, the first third of `git/repo.py`;
  `worker/tasks.py`, `celery_app.py`; `api/` (main, publisher, task routes, webhook, estimate query);
  `conflicts.merge_target`; `feedback.visible_feedback`.
- **Read in full (design):** all 12 files in `.claude/rules/`, `CLAUDE.md`, `docs/PHASE1_TESTING.md`.
- **Read as module docstring + function list only:** the resolver body, `render.py`, the sandbox/search/paths/gitproc
  tools, `finalize.py`, `pr.py`, `agent_context.py`, everything in `eval/harness/`, `shared/instances.py`,
  `process.py`, `tasks_recovery.py`, the GitHub client, `gateway/{config,recorder,redaction,vertex}.py`, the UI
  (router, templates, security), the 16 Alembic migrations, the rest of `infra/`, and all tests.

Where this guide says "per your notes" it comes from your session memory files, not from code. Where it says "per the
2026-10-25 session" it comes from a working session and is in no repo file (the database and `eval/runs/*/report.md`
back the benchmark numbers; the transcript analysis, the OpenRouter and NVIDIA live checks and the hand-computed
combined figure do not have a file behind them).

---

## 1. The pitch

### Thirty seconds

> repolace is an autonomous software-engineering agent platform. You register a GitHub repo, pick an issue and a
> target branch, and an agent retrieves the relevant code, edits it, runs the repo's own tests in a locked-down
> sandbox, scores the result against a baseline, and opens a PR if the checks pass. Agents like this are
> common. What I built the project around is the part most hobby versions skip: a benchmark that is hard to
> inflate, containment for running untrusted code, and reliability and cost accounting from the first run.

### Two minutes (add this)

> Concretely: a task is claimed from a Postgres queue by a Celery worker. It clones the repo, indexes it
> incrementally into pgvector, runs the test suite at the base commit to get a baseline, then a LangGraph agent
> with nine bounded tools edits the tree. Each attempt is committed and the suite is run in a rootless Docker
> container with no network. Scoring compares attempt to baseline: the target tests must flip to passing,
> nothing that passed may regress, no test file may be touched, and the pytest configuration must not have
> drifted. In benchmark mode the answer-key tests are laid over the sandbox export only, and a filter makes sure
> the agent never learns which of them are red. Every model call goes through a gateway that prices it, refuses
> unpriced models, enforces a per-task budget and records the full request and response.

### What makes it different (the thesis in `CLAUDE.md`)

The concept is not novel (SWE-agent, OpenHands, Aider, Devin). The claim is about **rigor**: a real benchmark
with success rate, cost and latency; isolation; retries; observability. If asked "why is this not just another
SWE-agent clone", the answer is the scoring rules, the no-oracle protocol and the security posture, not the agent loop.

---

## 2. The system on one page

```
 GitHub App ──webhook──▶  API (FastAPI)  ──insert queued row──▶  Postgres (+pgvector)
                           │  └ UI (Jinja2 + HTMX)                    ▲
                           └──publish task id──▶ RabbitMQ ──▶ Celery worker (prefork children)
                                                                │ run_task(task_id)  ← pipeline/run.py
        ┌───────────────────────────────────────────────────────┘
        ▼
  claim → preflight → embedder → clone → index → retrieve → baseline → branch
        → AGENT (LangGraph: localize → agent ⇄ verify) → review → merge_target → squash → push → PR → finalize
                     │ tools                │ model calls                 │ scored suite runs
                     ▼                      ▼                             ▼
              confined file/git ops   gateway (LiteLLM / Vertex REST)   verify: rootless Docker, no network
                                       price · budget · record          pytest plugin → JSONL → SuiteResult
```

### Packages and the dependency rule

| Package | Import name | One-line job | Approx. non-test lines |
|---|---|---|---|
| `shared` | `repolace_shared` | DB models, git wrapper and workspace, GitHub client, path guards, instance format | 4.2k |
| `verify` | `verify` | Sandbox backend, scoring rules, report parsing, hidden-test overlay | 4.1k |
| `agents` | `repolace_agents` | LangGraph graph, 9 tools, feedback filter, prompts, conflict resolver | 5.3k |
| `gateway` | `repolace_gateway` | LiteLLM wrapper: routing, price, budget, recorder, Vertex transport | 2.7k |
| `rag` | `retrieval` | tree-sitter chunking, local embeddings, pgvector index, hybrid search | 1.6k |
| `pipeline` | `repolace_pipeline` | `run_task`: the stages around the agent, gate, PR text, finalize | 4.7k |
| `worker` | `repolace_worker` | Celery app, task runner, sweeper and reaper | 0.8k |
| `api` | `repolace_api` | FastAPI routes, webhook, server-rendered UI | 3.7k |
| `eval` | `harness` | Instance selection, bench repos, sweep runner, gold validation, report | 6.4k |

About 33k lines of Python excluding tests, and about 3,800 test functions.

**Dependency direction** (enforced by review rounds and package metadata):
`shared ← verify ← agents`, `shared ← gateway ← agents`, and `pipeline → {agents, gateway, rag, verify, shared}`.
`agents` must not import `rag`, which keeps torch out of agent tests. Every package has a **distinct top-level import
name** because they once all used `app` and the editable installs shadowed each other silently.

### The nine places state lives

| State | Where | Notes |
|---|---|---|
| Task row | `tasks` | status, outcome, stop reason, diff, cost, PR, heartbeat, claimed_by |
| Every suite run | `task_test_runs` | raw pass/fail/skip/xfail sets, so scoring can be redone without re-running |
| Every model call | `llm_calls` | full request and response, tokens, cost, latency |
| Stage timings | `task_stages` | best-effort; the task row stays the source of truth |
| Code index | `code_chunks` | 768-dim vector + generated `tsvector`, HNSW and GIN indexes |
| Installations/repos | `github_installations`, `registered_repos` | `indexed_commit_sha`, `index_strategy` |
| Queue | RabbitMQ | `acks_late`, at-least-once |
| Dedupe marks | Redis | `repolace:swept:<task id>` for the sweeper |
| Run manifests | `eval/runs/<run>/manifest.json` | what a sweep was meant to be |

---

## 3. One task, start to finish

Entry: `run_task` (`pipeline/repolace_pipeline/run.py:1323`). Callers: the Celery task
`worker/repolace_worker/tasks.py::run_one` for product tasks, and `pipeline/cli.py` (`repolace-run-task`) for
benchmark rows. Both build their long-lived objects with `resources.build_resources` and hand them in, which is
why `run_task` owns no engine, client or backend.

**Before any stage:** `_claim` (`run.py:377`) does a conditional `UPDATE ... WHERE status='queued'` and sets
`claimed_by` and `heartbeat_at`. Zero rows updated means `TaskNotClaimable` or `TaskNotFound`. That conditional
claim is what makes at-least-once delivery safe. A background `_heartbeat` coroutine then bumps `heartbeat_at`
every 30 s. `_stage` (`run.py:296`) wraps each stage, writes a `task_stages` row on its own session, and
re-raises non-pipeline exceptions as `StageFailed(stage, message)` so `error_message` always says where it died.

| # | Stage | What happens | Fails as |
|---|---|---|---|
| 1 | `instance` | Benchmark only: `load_instance_by_id` (confined path), builds the `RepoSpec` from the instance's own spec | harness error |
| 2 | `preflight` | `get_installation`; needs `contents:write` and `pull_requests:write` | harness error, before any work |
| 3 | `embedder` | Warm the sentence-transformers model in a thread so a model download is its own stage | harness error |
| 4 | `clone` | Full non-shallow clone at the target branch. Benchmark: prune remote refs and **fail if base sha != the instance's base commit** | harness error |
| 5 | `index` | `reindex_if_stale` on its own session; advisory try-lock; waits up to 20 min if another task holds it | harness error if 0 chunks |
| 6 | `retrieve` | `build_query(title, body)` → `hybrid_search` (limit 8), using the strategy the index was **actually** built with | harness error if nothing matches |
| 7 | `verify_baseline` | Build the image once, run the suite at the base commit (attempt 0) | sandbox unavailable = harness error |
| 8 | `record_baseline` | Write the `task_test_runs` row | |
| 9 | `baseline_files` | `git ls-tree` of the base commit | |
| – | **inadmissible check** | `_inadmissible_instance`: unusable baseline, or a curated fail-to-pass test that was not red. Completes the task **without running the agent** | `completed`, no outcome |
| 10 | `branch` | `repolace/issue-<n>-<task hex>` | |
| 11 | `agent` | Inside `task_scope` + a `TaskBudget` whose clock starts here. `_run_agent` then `_check_against_scorer` | agent stops = `completed` |
| 12 | `review` | Rewind to the last **scored** commit if HEAD moved; read changed files and diff | |
| – | judge + gate | `_judge` (score + verdict), `_decide` (`pr_decision`) | |
| 13 | `merge_target` | Product tasks that would open a PR only: fetch the target, merge, resolve, re-score | `conflicting` |
| 14 | `squash` | `commit-tree` then move the ref (never reset first) | |
| 15 | `push` | Token refreshed just before; credential helper scoped to github.com | harness error |
| 16 | `pr` | Title/body from `pr.py`, sanitised | harness error |
| 17 | `finalize` | `write_terminal`: every column is a required keyword; conditional on `running` | |

The statuses: `queued → running → {pr_opened | completed | conflicting | failed}`.
**`failed` means exactly one thing: repolace itself broke.** An agent that ran out of budget, hit the step cap or
gave up is `completed` with an outcome and a stop reason. This is accounting, not politeness: the report counts a
`failed` row as a harness error and *removes it from the secondary denominator*, so mislabelling an agent failure
as `failed` would quietly raise the secondary score.

**Interrupts.** `except BaseException` in `run_task` writes `interrupted: <ExceptionName>` and re-raises, so a
cancelled task never stays `running` forever. The worker's time bound arrives as `WorkerTimeout`
(`worker/bound.py`), a `CancelledError` subclass on purpose so process-group kill handlers still run.

---

## 4. The agent (`agents/`)

### Shape: a hybrid graph (`graph.py`)

`localize → agent → verify → route → (agent | END)`. Built with LangGraph's `StateGraph`.

| Node | Line | Does | Does not |
|---|---|---|---|
| `localize` | 230 | Builds the system prompt and first user message (issue, repo overview, retrieved snippets, baseline summary) | call a model |
| `agent` | 245 | **The only node that calls a model.** A bounded tool loop: model → up to 8 tool calls → results → model | score anything |
| `verify` | 337 | Calls `deps.verify_attempt`, filters the result through `feedback.visible_feedback`, decides retry | |
| `route` | 386 | Retries only on a *fresh, not-clean, non-infrastructure* verification with attempts left | |

There is **no finalize node**. Scoring, the PR gate, squash, push and the final write need git, Postgres and GitHub,
so they live in the pipeline, after the agent returns an `AgentResult`.

State (`state.py`) is a frozen dataclass where **every field has a default** (LangGraph builds it as
`schema(**input)`). Dependencies travel in `RunContext`, not in state, because state is what a checkpointer would
persist and an LLM client is not.

**Recursion limit** = `4 * max_attempts + 8` (`graph.py:108`). The graph needs only about `2*attempts+1` steps, so
this is a tripwire: a routing bug becomes `GraphRecursionError` within a dozen steps instead of a model call per step
until the budget ends it. Deliberately **not** mapped to a stop reason: it means repolace is broken, so it fails the
task as a harness error rather than hiding inside the agent's denominator.

### Bounds (know these cold)

| Bound | Value | Enforced by |
|---|---|---|
| Scored attempts | 3 (`AgentLimits.max_attempts`) | `verify` node |
| Model calls per attempt | 40 (`max_steps_per_attempt`) | `agent` node → `STEP_CAP` |
| Tool calls executed per reply | 8 (`MAX_TOOL_CALLS_PER_REPLY`), rest answered "not executed" | `agent` node |
| Cost per task | $2.00 | gateway `TaskBudget` |
| Model calls per task | 150 | gateway |
| Wall clock for the agent stage | 3600 s, **started when the agent stage starts** | gateway; `_agent_budget` in `run.py:749` |
| Tool output | 8,000 chars | `ToolBox._cap` |
| Transcript | 400,000 chars, older tool results elided, newest 6 kept | `agent` node |
| Env overrides | `REPOLACE_AGENT_MAX_STEPS_PER_ATTEMPT`, `..._MAX_ATTEMPTS`, 1..200 | `AgentLimits.from_env` |

The budget is also checked **between tool calls** (`_raise_if_budget_reached`), because one reply's calls can each run
for minutes.

### Stop reasons (8, behind a CHECK constraint)

`submitted`, `step_cap`, `budget_usd`, `budget_calls`, `budget_wall`, `llm_error`, `no_change`, `max_attempts`.
`max_attempts` was added after review so "submitted clean" and "ran out of attempts still red" are not one bucket.
After a budget or LLM stop, **verify is skipped** (the tree may be half-edited). A budget stop does **not** append the
response that crossed the line: its tool calls can't run, and an assistant message with unanswered `tool_calls` is
rejected by providers on the next call.

### The nine tools (`agents/tools/`)

| Tool | What it does | Notable guards |
|---|---|---|
| `search_code` | Queries the pgvector index through an injected callable on its own short session | query 1–500 chars, limit 1–20 |
| `read_file` | Numbered lines | ≤400 lines/call, ≤1 MB files, `.git*` refused even for reads |
| `grep` | `git grep` over tracked files | pattern is the operand of `-e`, path after `--`, `--literal-pathspecs`; runs under `prlimit --as=512MiB --cpu=15:20`; regexes with backreferences or `{100+}` refused |
| `list_dir` | Entries to a depth | depth 1–3, ≤300 entries |
| `edit_file` | Exact, unique string replace (`replace_all` optional) | CRLF hint on mismatch; projected-size check *before* building the result |
| `create_file` | New non-protected file | ≤100k chars; **no new test files allowed** |
| `run_python` | Scratch script in the sandbox | no network, source mounted `:ro`, never the hidden tests, ≤120 s |
| `run_tests` | **Probe**: some visible tests, unscored | never the hidden tests; targets can't start with `-` or `@` |
| `submit` | Ends the loop | summary 1–4000 chars, no control chars |

`ToolBox` (`tools/base.py:471`) is where authority lives:

- It **refuses at construction any JSON-schema keyword it doesn't enforce** (`pattern`, `oneOf`, nested `properties`,
  union types), so a tool schema never advertises a bound nothing checks.
- `dispatch` turns a `ToolError` into a model-visible error result, but **lets every other exception propagate**.
  A bug in a tool must not be laundered into "the model was bad at using it". That is the same misattribution the
  scoring rules exist to avoid.
- Every path goes through `tools/paths.py::confine` (built on `shared/paths.py::resolve_within`): no absolute
  paths, no `..`, **no symlink anywhere on the path even if it lands inside the tree**, nothing in the `.git*` family
  (case-folded, HFS-ignorable code points stripped, NTFS short name `git~1`). For writes, additionally any component
  starting with `.`, root CI files (`Jenkinsfile`, `azure-pipelines.yml`, ...), and `ctx.is_protected`.

### Retry and feedback

After `submit` (or step cap) the pipeline's `AttemptScorer.verify_attempt` commits the tree and runs the **scored**
suite. `verify` then asks `visible_feedback` what the agent may be told. If there are visible regressions or new
collection errors and attempts remain, the agent gets one message and another attempt. Earlier tool outputs are
replaced by `[output from attempt N elided]` but the assistant messages and their `tool_calls` stay. Edits persist
between attempts: **nothing resets the tree** until the pipeline rewinds at the end.

A critical subtlety: `run_python` and `run_tests` make **checkpoint commits** before handing the tree to the sandbox
(the export refuses a tree that differs from HEAD). So HEAD can be ahead of the last scored attempt, and
**`AgentResult.last_attempt` is the only scored state**. The pipeline rewinds to it before diffing, squashing or pushing.

### Prompt design (`prompts.py`)

Authority comes from the system prompt and harness messages **only**. Everything else (issue, retrieved code, test
output, tool output, file names) is declared untrusted data, wrapped in blocks tagged with a **per-task random nonce**
(`<issue-3fa9...>`). The prompt deliberately omits the issue number, URL and `instance_id`, because a well-known
public issue ID is exactly what lets a model recall the upstream fix from training. `hints_text` (SWE-bench comments,
which often contain the fix) is not a field the agent's `IssueContext` even has.

### Conflict resolver (`resolver.py`, 600 lines)

A separate, self-contained loop used only by `merge_target`. Exactly five tools (`read_file`, `grep`, `list_dir`,
`edit_file` confined to the conflicted paths, `submit_resolution`). No `run_python`/`run_tests` (a mid-merge tree is
full of conflict markers and the checkpoint would commit them). It never touches git: the host re-checks for markers
and unmerged entries, and the host commits. Limits: 20 steps, 3 rounds, its own budget (half of what the agent has
left, ≤$0.50, ≤40 calls, ≤600 s).

---

## 5. Scoring: the heart of the claim (`verify/verify/scoring.py`)

Read this file until you can reproduce its logic from memory. Its docstring states the posture:

1. **Err toward FAILED.** A false negative costs a point. A false positive costs the whole claim.
2. **Report inadmissible instead of a verdict when the instrument failed.** "We never found out" is not "the agent lost".

### `score(baseline, attempt, changed_files, ...)` (line 556). First match wins; order is load-bearing.

1. `_preconditions` (line 504), shared with `agent_verdict` so benchmark and PR gate cannot disagree:
   1. **Disqualifying paths** → `FAILED` "diff touches test or config files". Checked *first*, with no run needed,
      so crashing the harness can't launder a test edit into an exclusion.
   2. Baseline has an `error` → **inadmissible**.
   3. Attempt hit an **infrastructure** error (daemon down) → inadmissible. A suite that timed out or was OOM-killed
      under the patch is the patch's doing → `FAILED`.
   4. Attempt `error` otherwise → `FAILED` "attempt unscoreable".
   5. **Fingerprint drift** (rootdir, watched ini options, registered plugins differ) → `FAILED`.
2. No curated list and nothing red at baseline → inadmissible ("cannot demonstrate a fix").
3. Compute `fail_to_pass`, `regressions`, `neutralized`.
4. `neutralized` non-empty → `FAILED` (baseline failures silenced rather than fixed).
5. `regressions` non-empty → `FAILED`.
6. With a curated list: every expected id must be in `attempt.passed` → `PASSED`, else `FAILED`.
7. Without: at least one fail-to-pass → `PASSED` labelled "(uncurated: not evidence this issue specifically was fixed)".

### The definitions

| Term | Definition (code) |
|---|---|
| fail-to-pass | `(baseline.failed ∪ baseline.xfailed) ∩ attempt.passed`, plus tests in modules that failed to import at baseline and now pass (`collection_fixed`) |
| regression | `baseline.passed − attempt.passed` (a set difference, so a **deleted** test, a test turned into a skip, or a module that stopped collecting all count) |
| neutralized | baseline-failing tests now skipped/xfailed/did-not-run, or that vanished |
| disqualified | changed paths that are tests/config (see below) |

### Bugs the audits found (know these stories; they show the thinking)

- **A false PASSED from ordinary skips.** fail-to-pass used `failed ∪ skipped`. A baseline `importorskip` that started
  passing for any reason scored PASSED with *nothing red*. Fix: the report parser separates `xfailed` from `skipped`;
  fail-to-pass joins on `failed ∪ xfailed`; `neutralized` counts xfails as silenced (otherwise marking a failure
  `@xfail` is the cheapest cheat); admissibility uses the same function so the two cannot drift.
- **The existence exemption was too wide.** `disqualifying_paths` spared files that existed at baseline and weren't
  collected (to spare `django/test/client.py`). That also spared `tests/helpers.py`, `__snapshots__/`, `cassettes/`.
  Fix: the exemption applies only if no *test-named ancestor directory* is one pytest collected from.
- **`lstrip("./")` ate the leading dot** of `.gitattributes` and `.coveragerc` so they matched nothing. `_normalise`
  now uses `removeprefix`. The unit test passed throughout because the bug lived in the seam between two functions.
- **`expected_not_red`**: if a curated fail-to-pass test already passes at baseline in our environment, every other
  condition is met by a no-op patch (even a comment). The pipeline checks this **before** the agent runs and completes the
  task inadmissible.
- **Fingerprint instability** (found only by running a real container): pytest names unnamed plugins by `id()`, so
  the plugin list changed every run and `score` would have returned FAILED for **every task**. The plugin now counts
  anonymous registrations instead of naming them.

### Two verdicts, one precondition set

`score()` is the benchmark's. It is inadmissible whenever nothing was red at baseline, which is the **normal** case
for a live issue (the suite passes; the bug just isn't covered). So `agent_verdict()` (line 671) is the product PR
gate: `ok` means only "no regression, silenced failure or new collection error was found", never "the issue is fixed".
The PR body says what was *not* verified.

### The PR gate (`pipeline/gate.py`)

Pure function, `pr_decision`. Shared rules first: switched off → no; no change → no (and `open_pr_on_failure` doesn't
override it); change over 10 files or 500 lines → no. Then **benchmark mode**: open only if `outcome == PASSED`, so the
number of PRs in a bench repo *is* the pass count. **Product mode**: open if the agent submitted and `verdict.ok`.
In benchmark mode the gate's reasons never name a test id.

---

## 6. The sandbox (`verify/`)

### What it executes, and why the threat model is subtle

It runs the repo's own test suite, which means `conftest.py` executes at *collection*, before any patch exists.
**Registering a hostile repo is the whole attack.** There are two separate dangers and a better sandbox only fixes one.

**Danger 1, containment.** `_containment_argv` (`backends/docker.py:145`) is shared by the pytest run and the
scratch-script run, so a flag can't be dropped from one and kept in the other:

`--rm --pull=never --init --network=none --read-only --tmpfs=/tmp:rw,nosuid,nodev,size=256m --cap-drop=ALL
--security-opt=no-new-privileges --user 65534:65534 --memory 2g --memory-swap 2g --cpus 2.0 --pids-limit 512
--ulimit nofile=4096 --log-opt max-size=10m max-file=1`

Each fails **open** if dropped (a missing `--network=none` is just a container with network and nothing looks
different), so `build_run_argv` is a pure function and its test asserts each flag separately. `memory-swap` equals
`memory` so a container can't exceed the cap by swapping. `--pull=never` so a missing image can't trigger a registry
pull on the one path that must have no network.

**Danger 2, the confused deputy.** Nothing escapes: the sandbox writes a file, exits, and the *host* later reads
it and acts. Examples demonstrated against this code: a `post-commit` hook run by the host's next `git commit`
(`--no-verify` doesn't suppress `post-commit`); `remote.origin.url` rewritten so the host's next `git push` sends the
installation token to an attacker's host; a tracked `.gitattributes` `filter=x` paired with the *operator's own*
`~/.gitconfig`. The fixes, in layers:

| Layer | Mechanism |
|---|---|
| The sandbox never gets `.git` | `export_tree` copies the index's blobs out |
| **Byte-identical export** | `git ls-files -s -z` + `git cat-file --batch` → write bytes. Not `archive` (honours `export-ignore`, so a repo could hide its own tests), not `checkout-index` (runs filters/eol: measured 72 of 110 files byte-different) |
| Symlinks/gitlinks refused loudly | Because "the bytes match the commit" isn't well-formed for them. Cost: a repo with any symlink can't be exported |
| Git config pinned | `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`, env built by **allowlist** |
| Credential helper scoped | Releases the token only for `protocol=https` + the expected host, taken from `GitRepo.credential_host`, not from the checkout |
| `require_trusted_config` | Before fetch/merge/stage/commit, refuses if `.git/config` has any key outside what `git clone` writes |
| Paths | Everything from a DB row, diff or model goes through `resolve_within`; symlinks refused |
| Report integrity | Host opens `report.jsonl` with `O_NOFOLLOW|O_NONBLOCK`, `fstat`s the descriptor, caps at 64 MiB, requires the per-run **nonce** on `start` and `session` records |

`tree_matches_head` (a precondition for export) uses `status --porcelain --untracked-files=no`. It used `diff-index
--cached`, which ignores the working tree, so an unstaged edit would have been tested as HEAD's content and the
result attributed to the agent's change: "a confidently wrong measurement, the worst shape this stage can fail in".

### The `Verifier` (`stage.py:177`)

| Method | Key | Overlay (hidden tests) | Recorded | Notes |
|---|---|---|---|---|
| `run(workspace, attempt)` | int | **yes** | by caller | Builds the image on first call; **second baseline raises** |
| `run_subset(targets)` | `probe-<n>` | **never** | no | Needs the env already built (`VerifierNotReady` otherwise); dirs discarded in `finally` |
| `run_script(code)` | `script-<n>` | **never** | no | Source `:ro`, script mounted at `/scratch/_repolace_script.py` |

**Environment is built once, from the base commit, and reused.** If an attempt could rebuild it, a newly failing
suite might be the patch or a dependency that resolved differently, and the baseline exists to remove that ambiguity.
The **image cache key** covers the Dockerfile, the plugin's bytes and the dependency *manifests*, never the tree.

### Pytest argv (every run gets these)

`--rootdir=/repo` (one node-id space), `-o cache_dir=/tmp/.pytest_cache` (otherwise the sandbox leaves a dir the
host can't delete), `-o addopts=` (unless the spec keeps it), `--continue-on-collection-errors` (more lenient than
SWE-bench: without it one broken import hides every result), plus `-p _repolace_report`.

### The plugin and parser

`verify/plugin/_repolace_report.py` records raw **events** as JSONL (`start`, `test` per phase, `collect` failures,
`files`, `session`). It is written for old Pythons (stdlib only) because it runs inside whatever the repo pins, and
it ignores nested pytest sessions (pytest's own suite runs pytest). `report.py::_verdict` aggregates phases per node:
any `failed` phase → failed (catches setup errors and teardown failures on a passing call); skip with `xfail` on the
skipping record → `xfailed`; else `skipped`; `call` passed → `passed`; else `did_not_run`. Pytest exit codes 0/1/6 are
scoreable; **5 (no tests collected) is not**.

---

## 7. The no-oracle protocol

In benchmark mode the hidden fail-to-pass tests are the answer key. If the agent can see which are red, the
benchmark measures how well it reads the key. Rule: **hidden FAIL_TO_PASS tests never reach the agent.**

| Mechanism | Where |
|---|---|
| Overlay goes on the **export**, never the checkout, so no tool can read the files | `Verifier.run` |
| Probes and scripts carry no overlay | `run_subset`, `run_script` |
| **Feedback filter is the only place a scored `SuiteResult` becomes model-visible text** | `agents/feedback.py::visible_feedback` |
| Filter by path (drops ids under an overlay path), rebuild result from empty `SuiteResult` copying only needed fields, compute counts and verdict from the **filtered** pair | same |
| An id is shown only if the filtered **baseline** produced it (the scored run executes the agent's code, which could invent ids) | `_universe` |
| Overlay mode = `instance_id is not None` **or** non-empty `hidden_paths`; defaults to **True** (fail closed) | `overlay_mode` |
| One neutral sentence for any errored scored run; baseline totals dropped | `COMPLETION_UNKNOWN` |
| Hidden test files are **protected-path-aware but invisible**: the refusal check is built from the baseline *minus* the overlay, so a refusal can't confirm a hidden file exists | `agent_context.protected_check` |
| One canonical phrase: "Some tests are not shown to you." | `TESTS_NOT_SHOWN` |
| **Tripwire tests**: sentinel planted in every field and searched for in the transcript; AST scan that prompt builders never touch `baseline`/`result`; metamorphic (change only hidden tests → identical transcript); graph-differential (hidden red vs green must not change attempts, stop reason or spend) | `agents/tests/test_graph_oracle.py`, `test_feedback.py` |

**Residual risks (stated plainly in the docs; be ready to state them):** a deliberately adversarial agent can still
signal through *which visible tests it breaks*, through the "did the scored run complete" bit, and, worst, through
**timing**: hidden tests sit readable in `/repo` during a scored run, so the agent's own code can sleep for a duration
that encodes their contents, and read the delay back through the clock. Roughly 10 bits per attempt. Not mitigated.
The closing design (feedback from an overlay-free run, scored run once at the end) was deferred because it doubles
suite executions and changes the `task_test_runs` contract. It matters if agents that might be adversarial are ever compared.

---

## 8. Retrieval (`rag/`)

| Step | File | Details |
|---|---|---|
| Chunk | `chunker.py` | tree-sitter-python. Chunk types: `function`, `method`, `class_skeleton`, `module`. A class becomes a **skeleton** (header + decorators + every method signature+docstring + attributes) plus standalone chunks for methods ≥2 lines. Walks through `if`/`try`/`with` so conditionally-defined symbols are found, but doesn't descend into function bodies. |
| Embed | `embed.py` | Local `flax-sentence-embeddings/st-codesearch-distilroberta-base`, 768-dim, **128-token window**. `lru_cache`d loader; dim checked against the column. |
| Index | `index.py` | `reindex_if_stale`: first time → full; strategy changed → full; same commit → 0; else `git diff --name-only -z old new`, delete+rechunk only changed `.py`, falling back to full if the old sha is unreachable. Embeds in batches of 256. Records commit **and** strategy in one statement. |
| Query | `query.py` | `build_query(title, body)` → two strings: **semantic** (title + first 1500 chars, ~what the encoder can read) and **keyword** (identifiers, dotted names and traceback frames, ≤32 terms, most specific first). All regexes are anchored to stay linear on attacker-controlled input. |
| Search | `retrieve.py` | Semantic arm: cosine distance with pgvector (HNSW). Keyword arm: OR of `plainto_tsquery` terms, `ts_rank`. Candidates = `limit × 5` each. |
| Fuse | `ranking.py` | Reciprocal rank fusion, `score = Σ 1/(60 + rank)` over both arms. |

**Concurrency:** `pg_try_advisory_xact_lock` keyed on the repo UUID, a **try**-lock, so a second task is refused at
once with `RepoIndexInProgress`. That is the normal case for a benchmark (3 runs of one instance), so `_index` in
`run.py` waits in 10 s steps up to 20 min. A session must not be reused across `reindex_if_stale` calls (it commits
and rolls back; `rollback()` expires ORM objects, and touching one afterwards raises `MissingGreenlet`, the one
defect that reached a real run).

**The known weak spot:** the encoder's 128-token window truncates **41.6% of chunks** in this repo's own index
(measured by tokenizing). Four strategies exist (`truncate`, `head_tail`, `windows`, `signature_docstring`), selectable
per index and recorded in `registered_repos.index_strategy` so old and new embeddings never mix. The retrieval eval
(`repolace-eval retrieval`) runs the grid against gold-patch hunks with a switch rule fixed in advance (≥10 pp
paired chunk-MRR gain with a bootstrap interval excluding zero). **No retrieval number has been measured.**
Default stays `truncate`.

**Limits:** Python only (a non-Python repo indexes to 0 chunks and the task fails loudly at `index`). The index is
per repo, not per commit, so concurrent tasks on different branches of one repo see each other's index.

---

## 9. The gateway (`gateway/`)

`LLMClient.complete` (`client.py:603`) is the **only** place the system talks to a model. Order of operations:

1. Reject reserved kwargs (`api_base`, `num_retries`, `stream`, credentials...) so the key can't be redirected.
2. `current_scope()` (raises `NoTaskScope` outside `task_scope`) → `budget.raise_if_reached()` (`>=`, "nothing left").
3. Route the stage; `preflight` → refuses an **unpriced** model or a missing key **before any spend**.
4. Call with retries (`_call_with_retries`): exponential backoff with jitter, honours `retry_after`, each attempt
   bounded by `min(request_timeout+5, remaining wall)`. A fallback model is tried only after *transient* failures.
5. Price, **charge the budget, then record the row**, then `raise_if_exceeded` (`>`). Charge-before-record so a
   failed DB write can't leave the in-memory cap blind. The call that crossed the cap is recorded and its response
   rides on `BudgetExceeded.response`.

Points worth remembering:

- **Pricing:** `[price]` in `models.toml` (USD per million tokens) wins; else LiteLLM's price map, **pinned** via
  `LITELLM_LOCAL_MODEL_COST_MAP=True` so a cost can't change because upstream edited a JSON file between two runs.
  Real tokens at ≤$0 on that map path is `UnpricedModelError`; an explicit `[price]` of 0 is accepted and means "free by
  decision" (only the NVIDIA entry uses it).
- **Prompt caching:** `with_cache_control` adds three Anthropic breakpoints (last tool, last system message, rolling last
  user/tool message) on copies, never the caller's dicts.
- **Vertex:** a Vertex API key doesn't fit LiteLLM's `vertex_ai/` (OAuth) or `gemini/` (AI Studio) routes, so
  `vertex.py` speaks `generateContent` over httpx. Endpoint fixed in code; key travels as a query param so every httpx
  failure is re-raised from the exception *type* only; thinking tokens added to output; Gemini 3's `thoughtSignature`
  is packed into the tool-call id (`call_<n>~<signature>`).
- **Failure kinds:** `auth`/`quota` are *provider refusals*, which are **harness errors**, not agent failures; the
  eval runner stops dispatching after two. `llm_error` (e.g. context overflow) is the agent's. A Vertex 429 that
  outlasts the 8 bounded retries is classed `quota`, so it costs the task (section 13b).
- **Two more providers (uncommitted on `main` as of 2026-10-25; read in `config.py`, `client.py`, `models.toml`).**
  `PROVIDER_KEY_FIELDS` gained `openrouter` (`OPENROUTER_API_KEY`) and `nvidia_nim` (`NVIDIA_API_KEY`), so the allowed
  providers are now anthropic, openai, gemini, minimax, vertex_ai, bedrock, openrouter, nvidia_nim.

  | Entry | Route | Price (USD per M tokens) | Status |
  |---|---|---|---|
  | `openrouter-deepseek-v4-pro-wafer` | `openrouter/deepseek/deepseek-v4-pro-0813`, pinned to endpoint tag `wafer/mxfp4` | $0.25 in, $5.00 out; `cache_read` set equal to input as an upper bound, because a response with cached tokens and no cache rate is refused as unpriced | Price is user-reported 2026-10-25, to be confirmed on the OpenRouter page. The OpenRouter account had no credits, so per the 2026-10-25 session it never ran. |
  | `nvidia-deepseek-v4-1-flash` | `nvidia_nim/deepseek-ai/deepseek-v4.1-flash`, NVIDIA's hosted OpenAI-compatible endpoint | 0 in, 0 out, 0 cache, because the hosted trial endpoint is free | Per the 2026-10-25 session the live call hung; only a few other NVIDIA models answered, and kimi-k2.6 and mistral-large-2 returned 404. No benchmark has run on it. |

  How the OpenRouter pin works: a model entry with `provider = "openrouter"` must set `openrouter_provider` (a tag
  matching `_OPENROUTER_TAG`, such as `wafer/mxfp4`; any other provider setting it is a load error). `client.py` then
  sets `kwargs["extra_body"] = {"provider": {"order": [tag], "allow_fallbacks": False}}` **after** merging the caller's
  `extra`, and `extra_body` was added to `_RESERVED_KWARGS`, so nothing a caller passes can unpin the host. The reason
  is the same as for the price rules: another OpenRouter host means another price and another quantisation, which is a
  different model in a benchmark. `wafer/mxfp4` is a quantised endpoint, so it is not the full-precision model.
  **The unpriced-model refusal still applies, but note what it keys on.** A model with no `[price]` table is priced by
  LiteLLM's map and real tokens at zero cost raise `UnpricedModelError` (`client.py`, around line 492). NVIDIA has an
  explicit `[price]` table of zeros, which the loader accepts (`_per_token` rejects only negatives), so it is a recorded
  decision that the endpoint is free, not a missing price. The consequence is that every cost figure for it is a floor
  of zero and cannot be compared with a paid model's; to compare cost you would have to re-price its token counts by
  hand.
- **Recording:** `llm_calls` stores request and response verbatim (redacted for known keys and credential shapes,
  NULs stripped). The cost: the table holds repo source, which matters for private repos.
- `cost_usd` is **NULL, not 0**, when nothing was priced ("free" ≠ "never priced").

---

## 10. Git and the workspace (`shared/git/`)

- `repo.py::GitRepo` is an async wrapper over the git CLI. Every command: base args (`core.hooksPath=/dev/null`,
  `core.fsmonitor=`, scoped credential helper, `gc.auto=0`), env by allowlist with config files pinned to `/dev/null`,
  its own **process group** (`start_new_session=True`), and on timeout/cancel `killpg` on the pgid **cached at spawn**
  (looked up later it can fail after reaping and leak the orphan).
- `clone()` asserts the result is **not shallow** (a shallow clone silently degrades every index to a full reindex).
  Transient failures are retried (3 attempts, 5 s and 20 s delays) by matching git's message.
- `TaskWorkspace` (`workspace.py:87`) steps: `start_agent_branch`, `export_tree`, `results_dir`, `record_attempt`,
  `rewind_to`, `review_diff`, `changed_files`, `baseline_files`, `target_moved`, `advance_base`, `commit_resolution`,
  `squash`, `push`. `task_workspace()` is an async context manager that clones into `mkdtemp`, removes the tree on
  exit (with an rmtree hook that chmods the parent for sandbox-owned dirs) and never lets cleanup mask the real error.
- **Squash:** compare `HEAD^{tree}` to `<base>^{tree}`, `commit-tree`, *then* move the ref. The obvious `reset --soft`
  then `commit` moves the ref first, so a failure in between strands the branch with every attempt unreachable, in a
  clone about to be deleted.
- `commit_resolution` is the **host's** check on a merge: paths confined, every unmerged path covered, no conflict
  marker lines, nothing outside the set edited.

### Merge-target flow (`pipeline/conflicts.py::merge_target`)

Runs only for product tasks whose gate would open a PR. Not moved → nothing. Moved but old base isn't an ancestor of the
new tip (force-push) → `conflicting`. Dirty tree → reset to the scored commit. `merge-tree` preview; >500 conflicted
paths → `conflicting`. `merge --no-commit`; preview and real merge must agree. Clean → commit. Conflicts → every path
must be both-modified or both-added UTF-8 text, then the resolver, then the host's `commit_resolution`. Merged tree
equals target's tree → `ALREADY_CONTAINED`. Otherwise `advance_base(tip)` and **one more scored attempt on the merged
tree**, and the gate is asked again. Known limit: the merged attempt is compared with the *old* baseline, so a target that
deleted a passing test reads as a regression and withholds the PR (cautious direction).

---

## 11. Worker and recovery (`worker/`)

- `run_task_job` is `acks_late`, `reject_on_worker_lost`. **Delivery is at-least-once; a task row is never re-run.** A
  redelivered message finds the row not `queued`, gets `TaskNotClaimable`, and is acked and dropped.
- One asyncio loop and one `Resources` per prefork child (built in `worker_process_init`; the embedder is warmed there,
  never in the parent, because torch and fork don't mix). A child that fails init raises `SystemExit(1)` rather than
  carrying on with no resources and acking every task into failure.
- Bound: `bounded(run_task(...), 11_700 s)` → `WorkerTimeout`. The number is **derived**: index-lock wait 1200 + env
  build 2400 + baseline 1800 + agent wall 3600 + one overrunning scored suite 1800 + 900 slack. Celery's hard limit is
  that + 600. RabbitMQ `consumer_timeout` must exceed the hard limit (14,100,000 ms, in `infra/rabbitmq.conf`, held by a test).
- **Refuses benchmark rows** (`eval_run_id` set): the eval runner owns those.
- **Sweeper:** republishes `queued` non-benchmark rows older than 5 min, ≤50/tick, at most once per 15 min per task
  (Redis `SET NX EX 900`), messages `expires` at the dedupe window, aborts the whole tick on the first broker/Redis error.
- **Reaper:** fails a `running` product row (no `eval_run_id`) whose `heartbeat_at` is older than 5 min as `runner: abandoned`. The staleness test is
  in the `UPDATE`'s own `WHERE`, so a heartbeat landing mid-check wins. It removes the task's containers by name prefix.
- Both run on a **separate `repolace.maintenance` queue** in a separate worker (exactly one replica), so a pool full of
  multi-hour tasks can't starve them.

---

## 12. API and UI (`api/`)

- FastAPI. Routes: `/repos`, `/repos/{id}/issues`, `/branches`, `POST /repos/{id}/tasks` (422 unknown branch, 409 if
  the issue already has a queued/running non-benchmark task on that branch, 502 on GitHub failure), `GET /repos/{id}/tasks`
  (keyset pagination on `(created_at, id)`, opaque base64 cursor), `/tasks/estimate` (p50/p90 over the last 30 priced
  tasks, falling back repo → model → none), `GET /tasks/{id}`, plus `/github/webhook` (HMAC-SHA256 checked with
  `compare_digest`) and a no-op `/github/callback`.
- **Publishing happens after commit and never fails the request:** the row stays `queued` and the sweeper republishes.
- **Benchmark scrubbing lives in `load_task_detail`** (`score_reason`, `patch_diff`, `changed_files`, `error_message`,
  stage errors → NULL for benchmark rows) because those can name hidden tests. One place, so no page can forget it.
- UI: server-rendered Jinja2 + HTMX, no node build. Live rows poll every 3 s; the fragment answers **HTTP 286** when the
  task is terminal, which tells HTMX to stop polling. CSP `default-src 'self'`, no inline script/style, same-origin
  check on the one POST, **Host allowlist** against DNS rebinding. **No authentication until Phase 4**, so it is
  localhost/tunnel only.
- Query indexes (migration 0015) are built `CONCURRENTLY` with `DROP INDEX CONCURRENTLY IF EXISTS` first, because a
  cancelled concurrent build leaves an INVALID index that still owns its name. A test replays the real statements under
  `EXPLAIN` to prove the indexes are usable.

---

## 13. The benchmark (`eval/`)

### Pipeline of stages (`repolace-eval`)

`select` → `fork` → gold validation (`enqueue --agent gold`, `run --agent gold`, `gold`) → sweep (`enqueue`, `run`) → `report`;
separately `retrieval`.

| Stage | What and why |
|---|---|
| `select` | Filters SWE-bench Verified: repo allowlist (pytest, pylint, requests, xarray, sphinx, seaborn, flask), complete rows, non-empty F2P, test patch touches only protected paths, **gold patch must be fixable by the agent's tools** (touches no protected path), buildable environment, no symlinks at base, patches apply, UTF-8. Deterministic: order by `sha256(seed:id)`, round-robin per repo, cap 6/repo. **The seed is a degree of freedom, so fix and record it first.** Patches applied to a **git index**, never a checkout. |
| `fork` | Creates private `repolace/bench-<id>` repos. Guardrails *are* the design: `check_bench_name` is the one decision, enforced again on every outgoing request and again where the push leaves the process; **never adopts or deletes a repo it didn't create** (marker description); Actions disabled before the first push. Token inline, never in `.env`. |
| Gold validation | Runs the *reference fix* through the real pipeline at $0, twice. Accept only if both pass, baselines are identical (flaky otherwise), every F2P id was collected and red. **If gold fails, the instance is dropped, not fixed.** |
| Sweep | `enqueue` writes the manifest atomically *before* inserting rows (idempotent via the partial unique index). `run` starts K subprocesses (`repolace_pipeline.cli`), each its own session; env allowlist; breaker stops after 2 provider refusals; `--max-total-usd`. A row is **never re-queued**. |
| Report | See below. |

### Report accounting (`report.py`; the claim)

- **Headline = `passed / planned`**, planned = manifest grid `instance_ids × range(runs)`. Every non-pass counts as a non-pass
  *whatever the reason*. `passed / admissible` is printed only as a labelled secondary.
- Missing/unfinished planned rows **withhold** the headline unless `--allow-partial`. No manifest → marked `UNANCHORED`.
  A run that mixes models withholds its headline.
- Interval: percentile **bootstrap over instances** (runs of one instance aren't independent). At N≈18 the 95% interval is
  roughly 30–40 points wide, and one instance moves the rate by 1/N.
- Model comparison: **exact McNemar** on discordant pairs, no difference of rates. 8:0 → p=0.008, 7:1 → 0.07, 6:2 → 0.29.
- Cost/latency are **censored** by the $2 cap and wall-clock limits.
- **Disclosures that must travel with any number:** the instances are a filtered subset of a public benchmark models have
  probably seen, so this is **not** the published Verified score; the issue text can carry upstream identifiers.

### The 18 instances

seaborn (2), flask (1), pylint (3: 4551, 4604, 4970), pytest (6), sphinx (6). Phase 1 headline model: Vertex Gemini 3.1 Pro.

### What real runs revealed (the war stories)

Gold validation found **12 of 18 instances failing for instrument reasons** on the first pass, none the instance's fault:

1. SWE-bench's `packages: requirements.txt` isn't a tracked file → name the real requirements file.
2. `setuptools_scm` needs `.git`, which the export deliberately lacks → `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_<DIST>`; and the
   generated `_version.py` was hidden by the bind mount → `RepoSpec.generated_files` laid over every export.
3. The report plugin read `Config.rootpath`/`inipath` (pytest ≥6) → fall back to `rootdir`/`inifile` for pytest 5.x.
4. pytest's own suite needs `pytester` and no plugin autoload (`addopts` is cleared by the sandbox).
5. sphinx 3.x needs `roman` (docutils stopped bundling it) → scoped to 3.x so validated 4.x envs weren't changed.
6. An unscoreable run kept only stdout; the sphinx failure was on stderr → capture separately.
7. SWE-bench truncates ids at the first space (`test_x[a:`) → `expand_truncated_ids` reads an unclosed `[` as a prefix.

And the fingerprint-by-`id()` bug and the `.pytest_cache` permissions bug from section 6.

Per your notes: `dry-4` (pytest-7236, 80 steps) passed for $0.56 in 15 min; at 40 vs 80 steps no outcome changed
(pylint-4604 and seaborn-3187 fail both ways). A phone-hotspot link caused clone timeouts at 900 s and pip stalls, which led
to the retry transport (#47) and pip timeout settings (#48). The sweep was paused until fast Ethernet (~2026-10-25)
and then run on 2026-10-23..25 (`sweep-4`, `sweep-4b`). **A Phase 1 headline now exists, with heavy caveats; read 13b
before quoting it.**

---

## 13b. What the Phase 1 run actually showed

Sources: `eval/runs/sweep-4/report.md`, `eval/runs/sweep-4b/report.md`, `../repolace-roles/eval/runs/sweep-r1/report.md`,
the development database (queried read-only on 2026-10-25), and the 2026-10-25 session where marked.

### Setup

Headline model Gemini 3.1 Pro on Vertex (`vertex-gemini-3-1-pro-preview`), single agent, 18 instances x 3 runs, `--no-pr`.
Per-task caps $2, 150 calls, 40 steps per attempt, 3 attempts. Pipeline commit `f02ccb3` for both sweeps.

### The numbers, and which are official

| Figure | Value | Where it comes from |
|---|---|---|
| `sweep-4` planned rows | 54 | manifest |
| `sweep-4` passed / failed (agent) / lost to provider errors | 29 / 13 / 12 | DB; the saved report was written mid-run and shows 11 harness errors and 1 row still queued, the DB now shows 12 `failed` |
| **`sweep-4` official headline** | **29 of 54 = 54%, interval 33%-72%** | report, marked PARTIAL because it was written while a row was still queued |
| `sweep-4` secondary (passed / admissible) | 29 of 42 = 69% (the 42 is 29 passed plus 13 failed; the 11 harness errors the report then listed are excluded, and the 12th lost row was still queued) | report |
| `sweep-4` spend | $32.31 | report |
| `sweep-4b` (re-run of the 12 lost rows) | 6 of 12 passed, 0 harness errors, spend $11.87 | report |
| **Combined, substituting the `sweep-4b` results for the 12 lost rows** | **35 of 54 = 64.8%, 95% bootstrap interval over 18 instances about 44%-83%** | the 35 is a DB count; the interval is not produced by the report tool (it never pools runs). I reproduced 44%-83% by resampling the per-instance pass counts below 20,000 times. |

The official `sweep-4` figure is the 54% one. The 64.8% is a **hand-built substitution** and must always be called that.

### Per instance, passes out of 3 (`sweep-4` and `sweep-4b` combined, from the DB)

| Passes | Instances |
|---|---|
| 3 of 3 (10) | pytest-5631, 6202, 7236, 7432, 7571, 7982; flask-5014; sphinx-9230, 9320, 9367 |
| 2 of 3 | pylint-4970 |
| 1 of 3 | seaborn-3069, sphinx-7462, sphinx-9602 |
| 0 of 3 | seaborn-3187, pylint-4551, pylint-4604, sphinx-7590 |

Read it as: ten instances are reliably solved, four are not solved at all, and the rest is noise. That is a bimodal
result, which is exactly why the bootstrap is over instances. Note `pylint-4551` is the one instance with a flaky first
gold validation (it was accepted by `gold-5`/`gold-6`), so a 0 there is the least informative.

### Why the 12 rows were lost: 429s from a Preview model

- Gemini 3.1 Pro is a **Preview** model on Vertex's Dynamic Shared Quota: shared global capacity, no per-project quota
  you can raise. Per the 2026-10-25 session, the Cloud console showed no quota near its limit.
- The lost rows all read `LLMCallError: provider quota exhausted ... RESOURCE_EXHAUSTED, HTTP 429` after 8 retries
  (the retry window had already been lengthened in #50 to about 3-6 minutes, after `sweep-2` lost three tasks, and later to 21 tries over about 15-30 minutes).
- Per the session, a bare "say ok" probe sometimes passes and real tasks then get 429s, so a probe is an unreliable
  health signal. `scripts/wait_for_pro_then_run.sh` therefore asks for three successes in a row.
- The runner stops dispatching after two provider refusals in one invocation, and a failed row is **never re-queued**
  (`task_test_runs` is unique on task and attempt). Both are deliberate; together they mean a throttled provider
  turns directly into lost planned rows.

### The re-run mechanism (`sweep-4b`)

A new run id, plus an optional manifest key `rows`, plus `repolace-eval enqueue --eval-run-id sweep-4b --rows 'inst#idx,...'`.
Code: `eval/harness/run_manifest.py` (`planned_pairs()` returns `rows` when set) and `enqueue.py`. Documented in
`.claude/rules/benchmark.md`. **Uncommitted on `main` as of 2026-10-25.** The report for `sweep-4b` covers those 12 rows
alone, flags `UNEQUAL RUNS PER INSTANCE`, and its "3 runs each" line is not meaningful. The two runs are never pooled
by the tool, so any combined number is a statement you made by hand.

### Stop reasons: what `step_cap` passing means

Finished tasks in `sweep-4` + `sweep-4b` (54 tasks, DB):

| Stop reason | Tasks | Passed | Failed |
|---|---|---|---|
| `step_cap` | 28 | 11 | 17 |
| `submitted` | 26 | 24 | 2 |

A `step_cap` task can be `passed` because the two fields answer different questions. The stop reason says why the
agent's loop ended. The outcome says whether the scorer's criteria hold for the tree that was left. At the step cap the
pipeline still commits whatever tree exists and runs the scored suite on it, so a half-finished-looking agent that had
already made a correct edit scores `passed`. Budget stops (`budget_usd`, `budget_calls`, `budget_wall`) and `llm_error`
skip verification and cannot pass. In **product mode** a PR needs the agent to have submitted (`pr_decision`), so 11 of
these 54 passes would not have produced a PR. State that when quoting the pass rate as a "PR rate".

### Cost, calls, latency

From the DB, over the 54 finished (`completed`) tasks: 34.9 model calls per task, about 749k input tokens (65% served
from cache) and about 11k output tokens per task, about $0.75 per task, median latency about 1,106 s (18 minutes).
Over all 66 task rows including the 12 lost ones the figures are 30.8 calls, 653k input (64% cached), 9.7k output,
$0.67 per task. The 2026-10-25 session quoted about 34 calls, 684k, 10k and $0.69: those sit between the two
populations, so say which population you mean. Total spend: `sweep-4` $32.31, `sweep-4b` $11.87. Cost and latency are
censored by the caps and include contention from parallel runs (the report says so).

### What the step-cap failures looked like (per the 2026-10-25 session; not in any repo file)

From `llm_calls` response tool calls on the 11 step-cap failures: the first edit came at about step 23 (passing tasks:
about steps 7 to 9); `run_tests` plus `run_python` were about 28 of 40 steps and reading or searching about 9; 7 of the 11
were still editing in the last 10 steps. The reading is that failing tasks explore and test for too long before they
commit to an edit, not that the step cap is too low. That is a hypothesis; the nudge experiment below tests it.
Per your notes, `dry-4` found that 40 versus 80 steps changed no outcome on the instances it covered.

### `sweep-5`: Gemini 3.8 Flash

A single-agent run started and stopped by the user. In the DB: 8 passed, 23 failed, 3 harness-failed, 20 still queued;
8 of 31 finished = 26%. It was judged the wrong model for the headline and is **not** a headline (spend $7.50, but the
run is incomplete and unreported). It is evidence that Flash-tier is far below Pro on this set, not a measured number.

### The role split (Phase 2, branch `phase-2/agents`, flag `--roles`)

| Run | What | Result |
|---|---|---|
| `sweep-r1` (role split as built) | Stopped after 10 of 54 rows | 2 passed, 5 failed, 3 harness errors (`runner: interrupted`); of 7 scored tasks the stop reasons were 4 `no_change`, 3 `step_cap`. The report is `PARTIAL (10 of 54)`, so 4% is meaningless. |
| `sweep-r2` (planner fix, commit `90c0be7`) | Stopped after Vertex 429s and then by the user | DB: 3 tasks finished (2 `no_change`, 1 `submitted`), 7 failed (provider), 0 passes |

Per the session, the diagnosis for `sweep-r1`: in all 7 finished tasks the planner called `submit_plan` zero times. It
used its whole 10-step cap exploring, so the editor started with no plan and 25 steps. The fix (`90c0be7`, in
`agents/repolace_agents/roles.py` and `role_prompts.py`): the planner explores for plan-1 steps and its last step is a
hand-over call that offers only `submit_plan`. Per the session, in `sweep-r2` the planners submitted 5 of 5, but two
finished tasks still ended `no_change` because the editor never edited.

Conclusion on record: **not enough evidence either way, and the split is not merged to `main`.** Your rule is to merge
it only if it beats, or at least does not regress, the single agent on the same instances. Do not claim the split "does
not work" or "works".

### Experiment in progress (no results)

An early-edit nudge on the single agent (branch `exp/early-edit`, worktree `../repolace-early`, planned run id
`sweep-e1`) is planned or running. There is no `sweep-e1` row in the database yet, and its worktree has no `eval/runs`
directory. Give no result for it.

### Embedding truncation: still unmeasured

Per the session, `repolace-eval retrieval --plan` was run (counts only, no embedding of the grid): first-index chunk
counts seaborn 2,637, flask 972, pylint 6,503, pytest 4,055, sphinx 6,462, total 20,629 over 5 upstream repos and 18
instances, none skipped, 20 index passes against the cap of 35. The real grid has **not** been run, so no retrieval number
exists, and every benchmark repo (including the 29/54 and 35/54 runs) is indexed with `truncate`, the strategy with the
41.6% truncation rate (section 8).

### What the result does and does not support

It supports: the pipeline runs end to end on real issues at about $0.7 per task; the agent solves ten of these 18 instances reliably. It does not support: any claim
about unseen issues (contamination, section 16), a model comparison (one model), or a precise rate (interval about 30-40
points wide even before the substitution).

---

## 14. Security model on one page

| Threat | Defence | Where |
|---|---|---|
| Hostile repo's `conftest.py` runs code | Rootless Docker, `--network=none`, caps dropped, read-only root, resource caps, uid 65534 | `docker.py` |
| Sandbox plants hooks / rewrites remote | Sandbox gets no `.git`; host never reads anything it executes | `export_index_to` |
| Host runs filter/textconv from tracked `.gitattributes` | Byte-identical export; git config files pinned to `/dev/null` | `repo.py` |
| Installation token exfiltration | Credential helper scoped to host+protocol; token in env var, never argv or URL | `_CREDENTIAL_HELPER` |
| App private key leaks to subprocesses | Env **allowlists** for git, docker, eval children | `sanitized_*_env` |
| Prompt injection via issue/code/test output | Nonce-delimited blocks, closers neutralised, tool-authority bound, 8 calls/reply | `render.py`, `ToolBox` |
| Agent writes tests/config/CI | `confine` + `is_protected_path` (baseline-aware) + scorer `disqualifying_paths` | tools, `scoring.py` |
| Path traversal / symlink | `resolve_within`, symlinks refused | everywhere |
| Regex DoS in `grep` | `prlimit`, pattern refusals, process-group kill | `gitproc.py` |
| Report forgery/substitution | `O_NOFOLLOW`, regular-file check, size cap, run nonce | `report.py` |
| Agent's summary pings people / leaks via image URL | Sanitised, then quoted in a fenced block | `pr.py` |
| Runaway cost | Per-task budget in the gateway (not the agent) | `budget.py` |
| Wrong price ⇒ wrong benchmark | Refuse unpriced models; pin the price map | `client.py` |
| DNS rebinding / CSRF on UI | Host allowlist, Origin check, CSP | `ui/security.py` |
| **Known, unclosed:** agent edits a non-protected build file (`Makefile`, `noxfile.py`) that CI runs; timing channel | Positional: bench repos private with Actions off; don't point product mode at repos with secret-bearing push CI | `prompt-injection.md` |

---

## 15. Decisions and the reasons behind them

| Decision | Why | Revisit if |
|---|---|---|
| One ephemeral clone per task | Chunks live in Postgres, so deleting the tree doesn't lose the index; full history keeps `git diff old new` working | clone cost dominates (then partial clone) |
| pgvector, not a vector DB | Repo-sized corpora | demoing sharding |
| Local embeddings | Re-indexing would otherwise be the dominant cost; deterministic across runs; no rate limit | image size (torch ~2–3 GB) or retrieval plateaus |
| LiteLLM gateway **from day one** | Cost not measured from the first run can't be backfilled; unrepeatable measurement | – |
| Docker via **CLI**, not SDK | "The argv *is* the security policy": worth being a value you can print and assert | – |
| Rootless Docker | A rootful daemon is root-equivalent | gVisor/Kata when strangers can register repos |
| Store raw test sets, not derived lists | Scoring rules are young; storing inputs lets you rescore | – |
| One agent, not planner/editor/reviewer | Split after seeing where one overloads (`phase-2/agents` is that experiment) | – |
| LangGraph kept | Per `CLAUDE.md`, least likely to beat a hand-rolled loop; value is the conditional retry edge and recursion guard | Phase 5 |
| `FAILED` = repolace broke | Report accounting (section 3) | – |
| Hidden tests on export only | Structural no-oracle | – |
| Headline over **planned** | Excluding rows after seeing results is cherry-picking | – |
| Task rows never re-queued | `task_test_runs` unique on `(task_id, attempt)`; a re-run would mix evidence | – |
| Postgres for run state, no result backend | Outcome lives on the row | – |
| Lost rows are re-run as a **new run id**, never into the old one (`rows` manifest key, uncommitted) | The old run's headline stays honest and unedited; the two are never pooled by the tool | a cleaner way to report a pooled figure is built |
| OpenRouter calls pinned to one endpoint tag, fallbacks off; `extra_body` reserved (uncommitted) | Another host is another price and quantisation, i.e. a different model | – |
| NVIDIA entry priced at an explicit 0 (uncommitted) | The hosted endpoint is free; zero is recorded as a decision, not a missing price | a paid tier is used |
| Role split stays unmerged | Merge only if it beats or at least does not regress the single agent on the same instances; the evidence so far is inconclusive | `sweep-r` runs finish |

---

## 16. Honest weaknesses (have these ready; interviewers respect them)

1. **The headline is real but soft.** Official `sweep-4`: 29 of 54 = 54% (interval 33%-72%), with 12 of 54 rows lost to
   Vertex 429s. The 35 of 54 = 64.8% figure (interval about 44%-83%) is a hand substitution of the re-run rows for the lost
   ones, not a tool output. One model, one pass over the set, 18 instances, probable contamination.
2. **N is tiny.** 18 instances → a ~30–40 pt interval. One instance is ~5.5 points. State it before anyone asks.
3. **Contamination.** Subset of public SWE-bench; models may have seen it. Controls were *recommended but not implemented*
   (post-cutoff issue set, diff-similarity to gold, "edits before exploring" signature).
4. **Selection bias.** The simple-fix filter means the set skews to pure source edits; the seed is a degree of freedom.
5. **Python-only and pytest-only.** Retrieval indexes only `.py`; scoring assumes pytest and its plugin.
6. **The timing side channel** (section 7) is open.
7. **Retrieval quality is unmeasured** and 41.6% of chunks are truncated by the encoder window.
8. **Mostly verified by tests the same toolchain wrote.** The defects that mattered (fingerprint `id()`, `.pytest_cache`
   perms, `MissingGreenlet`, tracked-symlink export refusal) were found by **running things**, not by unit tests. Say
   this, and say what you did about it (gold validation before any agent spend).
9. **No auth** on API/UI until Phase 4.
10. **Compose deployment** (`worker`, `worker-maintenance`, `rabbitmq.conf`) was validated with `docker compose config`
    only, never started.
11. **Scope-cap and CI-file gaps:** any non-protected file is writable, including build files CI executes.
12. **Cost data is better but not clean.** About $0.67 to $0.75 per task over 54 to 66 task rows on one model, but the
    figures are censored by the $2 cap, include sweep contention, and Vertex prices were read from AI Studio, not the
    Cloud console. The NVIDIA entry's zero cost is not comparable with anything.
13. **A Preview model made the benchmark unreliable.** 12 of 54 planned rows were lost to quota errors that no setting of
    ours could prevent, and a probe cannot predict them. The harness treats this correctly (harness error, never an agent
    failure) but the cost is a headline that needs a footnote.
14. **Step-cap passes.** 11 of the 35 passes in the combined `sweep-4` + `sweep-4b` figure ended on `step_cap`, not
    `submitted`. They are valid passes under the scorer, but in product mode they would not have opened a PR.
15. **Retrieval and embedding truncation still unmeasured**, and every benchmark repo is indexed with `truncate`.
16. **The role split is unproven.** Two partial runs found and fixed one real bug, then ran out of clean evidence.
17. **The new providers are mock-tested only** in the repo. OpenRouter never ran (no credits); the NVIDIA live call hung
    (per the 2026-10-25 session). The `rows` re-run code and the gateway changes are uncommitted.

### Stale statements that were in the docs (fixed 2026-10-26)

`CLAUDE.md` "Phase 1 status", `README.md` (agent and harness rows, migrations count, compose worker description,
"What comes next"), `.claude/rules/benchmark.md`, `.claude/rules/open-questions.md` and `docs/PHASE1_TESTING.md`
all said no real run or sweep had happened. They now state the sweep-4 and sweep-4b results and point to
`BENCHMARK/REPORT.md`. Still open: confirm Vertex prices in the Cloud console, and a run
that opens real PRs.

---

## 17. Branches and history in one table

Checked with `git branch -a` and `git worktree list` on 2026-10-25.

| Ref | Meaning |
|---|---|
| `main` | Everything above, at `f02ccb3` plus uncommitted work (gateway providers, the `rows` re-run mechanism, `scripts/sweep_dashboard/`, `scripts/wait_for_pro_then_run*.sh`, this guide) |
| `origin/phase-2/agents` | Unmerged **by design**: the `roles` flag adding planner/reviewer/debugger nodes, off by default. Now at `90c0be7` = the old branch plus `main` merged plus the planner hand-over fix. Fast-forward pushed. The local `phase-2/agents` was deleted. |
| `roles-sweep` (local) and worktree `../repolace-roles` | The checkout the role-split sweeps ran from, at `90c0be7`; holds `eval/runs/sweep-r1` and `sweep-r2` |
| `exp/early-edit` (local) and worktree `../repolace-early` | The early-edit nudge experiment (run id `sweep-e1`), currently at `f02ccb3`, no commits of its own yet; no results |

Deleted since the 2026-10-19 version: local `fix/vertex-429-retry-window`, `fix/pip-network-tolerance`,
`fix/github-client-retry` (merged as #50, #48, #47), `tmp/p2-merge` and the local `phase-2/agents`; on GitHub
`fix/vertex-429-retry-window` and `claude/project-thread-fjf0bi` (one commit, `4e2edf5`, "Wire the merge-conflict
resolver into the pipeline", superseded by #28/#45). The deletions are from the 2026-10-25 session; the table above is
what the commands show now.

Eras: scaffolding (Aug 23–25) → RAG (Aug 25–26) → decisions-as-docs (Aug 26–28) → verify sandbox + scoring hardening
(Aug 28–30, the densest) → a month gap → Phase 1 parallel streams (PRs #1–16, Oct 1–4) → benchmark data + gold fixes
(Oct 5) → Phase 2 + hardening (PRs #18–48, Oct 14–18).

---

## 18. Likely questions, with the answer shape

**Q: Walk me through what happens when someone clicks "start" on an issue.**
Section 3. Key sentences: API inserts a queued row and publishes after commit; worker claims conditionally; clone,
index, retrieve, baseline, agent, rewind to last scored commit, judge, gate, merge target, squash, push, PR, one
conditional terminal write.

**Q: How do you know the agent actually fixed it and didn't game the tests?**
Baseline → fail-to-pass/pass-to-pass; deleted or skipped tests count as regressions; baseline failures silenced is a
fail; test and config paths disqualify the whole diff; fingerprint drift fails; `expected_not_red` stops no-op patches
scoring; for benchmarks the hidden tests aren't visible to the agent. Then give the limit: no path rule can tell a real fix
from special-casing the input, which is why curated ground truth and human diff reading exist.

**Q: What stops the agent seeing the answer key?**
Section 7. Lead with "overlay on the export only; one filter is the only path from a scored result to model-visible text;
tripwire tests enforce it". Then volunteer the timing channel.

**Q: How is untrusted code contained?**
Section 6, two dangers. If you only say "Docker", you've missed the better half: the confused deputy.

**Q: Why did you build a gateway instead of calling the provider?**
Cost and trajectory are the benchmark's evidence and can't be recovered later; unpriced = hard error; budget in the
gateway because an agent policing its own budget has to be trusted to ask.

**Q: Why `failed` vs `completed`?** Accounting for the headline vs secondary denominators (section 3).

**Q: How does it behave when a worker dies mid-task?** Heartbeat every 30 s; reaper fails stale `running` rows; container
cleanup by name; startup sweep of old `repolace-task-*` dirs; never re-run a row. The cost: a dead worker's task is lost, not retried.

**Q: How does indexing stay fresh and safe under concurrency?** Section 8: try-lock, bounded wait, incremental diff,
strategy recorded with the commit.

**Q: How would you scale it?** Honest: stateless workers scale horizontally; the shared bottlenecks are the per-repo index
lock, the local docker daemon per worker host, embedding on CPU, and provider rate limits (Vertex 429s forced `-k 2`).
Next steps in the docs: k8s, OTel, replace sandbox with a custom Go implementation.

**Q: What would you do differently?** Cheap-first retrieval eval before agent spend; run gold validation earlier; keep docs
in lockstep with state (you have stale claims); consider the overlay-free feedback design if comparing agents that may be adversarial.

**Q: Your headline is 35 of 54. Where does that come from?**
It is a substitution, not a tool output. `sweep-4` planned 54 rows and passed 29; 12 rows were lost to Vertex 429s and
the report counts them as non-passes (official figure 54%). I re-ran exactly those 12 as `sweep-4b` (6 passed) and
swapped them in by hand: 29 + 6 = 35 of 54 = 64.8%. Caveats to say unprompted: the report never pools runs, so the
interval (about 44%-83%, my own bootstrap over the 18 instances) is not from the tool; the re-run rows ran later, under
different provider conditions; I only re-ran the rows that failed for provider reasons, which is defensible because they
had no agent outcome, but it is still choosing which rows to repeat; 11 of the 35 ended on the step cap; N is 18; the
set is probably contaminated. Both official reports stay unedited.

**Q: What did a 429 from a Preview model teach you?**
That the harness accounting was right and the infrastructure was the weak part. Provider refusals are harness errors,
not agent failures; the headline counts them as non-passes anyway; rows are never re-queued; the runner stops after two
refusals so a throttled provider cannot burn the whole sweep. What it did not give me was a way to predict throttling:
Dynamic Shared Quota has nothing to raise, the console showed no quota near its limit, and a "say ok" probe can pass
just before real tasks fail (per the 2026-10-25 session). So I added the re-run-as-new-run mechanism and a wait script
that wants three probe successes, and I would say plainly that a benchmark on a Preview endpoint needs a footnote.

**Q: Why did you not merge the role split?**
Because my rule is that it must beat or at least not regress the single agent on the same instances, and I do not have
that evidence. `sweep-r1` found a real bug (the planner never called `submit_plan`, so the editor ran with no plan and
4 of 7 finished tasks ended `no_change`). I fixed it (`90c0be7`). `sweep-r2` then finished only 3 tasks before 429s and
my stopping it, with 0 passes, and two still ended `no_change`. That is not enough to say the split is worse or better.
It stays on `origin/phase-2/agents`, off by default, proven not to change the single-agent path.

**Q: How would you make the headline cleaner?**
Re-run the lost rows as a separate run, which I did, and report both numbers side by side instead of a substitution:
the official partial run and the re-run, each with its own denominator. Better still, make the pooling a supported report
mode that states the rule (a provider-lost row is replaced once by its re-run) so the combined number comes from the tool.
Then run on a non-Preview endpoint or a second provider, add a post-cutoff issue set for contamination, and report
`submitted` passes separately from `step_cap` passes.

**Q: How did you build this?** The repository was written with AI assistance. Have your own honest answer ready about what
you decided, what you reviewed and what you can reproduce. This guide exists to close the gap between "it exists" and
"I can defend it".

---

## 19. Glossary

| Term | Meaning |
|---|---|
| Instance | One benchmark problem: repo, base commit, issue text, hidden tests, F2P/P2P lists, gold patch |
| Gold patch / gold run | The human fix; running it through the real pipeline validates the instance at $0 |
| Oracle | Anything that reveals the answer (gold patch, F2P ids, hidden test files) |
| Overlay | Hidden test files laid over the sandbox export for scored runs |
| Baseline | Suite run at the base commit (attempt 0) |
| Scored attempt / probe / script | Recorded run with overlay / unscored `run_tests` / unscored `run_python` |
| Fingerprint | rootdir + watched ini options + registered plugins; must match baseline↔attempt |
| Inadmissible | The instrument failed; excluded from the secondary rate, never from the headline |
| Neutralized | A baseline failure silenced (skip/xfail/vanish) instead of fixed |
| Planned | The manifest's `instances × runs` grid; the headline denominator |
| Sweep | The full benchmark run |
| Dry run | A small real run (per `docs/PHASE1_TESTING.md`: 3 instances, `-k 1`, `--no-pr`, ≤$3) to measure cost and time before the sweep |
| Smoke test | One instance, one run, `--no-pr`, ≤$1 |
| Confused deputy | Trusted code (the host) tricked into acting on a file untrusted code wrote |
| RRF | Reciprocal rank fusion, `Σ 1/(60+rank)` |

---

## 20. Cheat sheet

```bash
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock
uv sync --all-packages                      # plain `uv sync` prunes workspace members
uv run ruff check .
find . -name 'test_*.py' -not -path './.venv/*' -not -path './*/.venv/*' | xargs -n1 basename | sort | uniq -d   # must print nothing
alembic check                               # after touching models
scripts/phase1_preflight.sh                 # read-only
uv run --all-packages repolace-eval enqueue --eval-run-id smoke-1 --agent llm --model vertex-gemini-3-1-pro-preview --instances pallets__flask-5014 --runs 1
uv run --all-packages repolace-eval run     --eval-run-id smoke-1 --agent llm --model vertex-gemini-3-1-pro-preview -k 1 --max-total-usd 1 --no-pr
uv run --all-packages repolace-eval report  --run sweep-1 --gold-run gold-3 --gold-run gold-4
```

### Operating notes (added 2026-10-25)

- **Live view of a sweep:** `python3 scripts/sweep_dashboard/server.py <run-id> <port>` (binds 127.0.0.1 only,
  reads the database through the compose Postgres container every 15 s, so `DOCKER_HOST` must be set). Share it with
  `tailscale serve --bg <port>`. Your decision: dashboards are served locally, never as claude.ai artifacts.
  Uncommitted.
- **Wait for Pro, then run:** `scripts/wait_for_pro_then_run.sh <run-id> <max-usd>` probes Vertex Pro, needs 3 successes
  in a row 60 s apart (sleeps 10 minutes after a failure), then `exec`s one runner with `-k 1 --no-pr`.
  `scripts/wait_for_pro_then_run_roles.sh <checkout_dir> <run-id> <max-usd> [runners=3]` does the same from another checkout (the roles worktree) and starts that many runners.
- **Two or three runners on one run id are safe:** the claim is conditional, so a runner that loses a row exits 3 quietly.
- **Settle dead rows:** `repolace-eval run --mark-abandoned` marks stale `running` rows `runner: abandoned`.
- **Re-run lost rows:** `repolace-eval enqueue --eval-run-id <new> --rows 'inst#idx,...' ...` (new run id every time).
- **One worktree per experiment** (`../repolace-roles`, `../repolace-early`): the editable install runs the code of the
  checkout it was synced in, so switching branches in one directory under a running sweep changes the code under it.

Env switches worth remembering: `REPOLACE_TEST_DB_SUFFIX`, `REPOLACE_TEST_DB_REQUIRED=1`, `REPOLACE_TEST_DOCKER_REQUIRED=1`,
`REPOLACE_TEST_EVAL_REQUIRED=1`, `GATEWAY_STAGE_MODELS='{"agent":"<key>"}'`, `WORKSPACE_PARENT_DIR`, `WORKER_ROLE`.

---

## 21. Study plan: files in the order that builds understanding

Time estimates assume you read for comprehension, not skimming.

| # | Read | Lines | Why |
|---|---|---|---|
| 1 | `CLAUDE.md`, `.claude/rules/agent.md`, `success-criteria.md`, `no-oracle.md` | ~60 KB | The reasoning before the code |
| 2 | `verify/verify/scoring.py` | 756 | The claim |
| 3 | `verify/verify/protocol.py`, `stage.py`, `backends/docker.py` (to line 420) | ~1.1k | What a "run" is |
| 4 | `verify/verify/plugin/_repolace_report.py`, `report.py::_verdict`, `parse_report` | ~500 | How raw pytest becomes sets |
| 5 | `pipeline/repolace_pipeline/run.py` | 1415 | Everything wired together |
| 6 | `agents/repolace_agents/graph.py`, `contracts.py` | ~770 | The loop |
| 7 | `agents/repolace_agents/tools/base.py`, `paths.py`, `files.py` | ~1.3k | Authority as code |
| 8 | `agents/repolace_agents/feedback.py` | 615 | The no-oracle filter |
| 9 | `gateway/repolace_gateway/client.py`, `budget.py` | ~1.1k | Cost and calls |
| 10 | `rag/retrieval/*` | ~1.6k | Retrieval |
| 11 | `shared/repolace_shared/git/repo.py`, `workspace.py` | ~1.9k | Git hardening |
| 12 | `pipeline/repolace_pipeline/conflicts.py`, `agents/.../resolver.py` | ~1k | Phase 2 |
| 13 | `worker/repolace_worker/*`, `shared/.../tasks_recovery.py` | ~1k | Recovery |
| 14 | `eval/harness/report.py` (docstring + `classify`, `_headline`), `gold.py` | skim | The accounting |
| 15 | `git log --first-parent main` and the fix-up commits in Era 4 (Aug 28–30) | | How the bugs were found |

After step 5 you can explain the system. After step 8 you can defend it.
