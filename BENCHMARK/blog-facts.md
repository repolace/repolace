# repolace blog fact sheet

Collected 2026-10-26 on `main` @ `0e9692d`; refreshed 2026-10-28 on `main` @ `7f4cd9e` (docs commit; sections 1, 2a, 2d-2f, 3, 4a-4c are unchanged by it and were not re-run, except where marked REFRESH 2026-10-28). Read-only: SELECT queries, `git log/show`, file reads. Rows are `fact | value | source`. Anything not produced by a command or file line read in this pass is marked UNVERIFIED.

**Note, 2026-11-14:** this sheet is a snapshot of the dates above and was not refreshed when it was committed. The files it calls untracked (this sheet, `scripts/visual_report/`, `scripts/wait_for_pro_then_run*.sh`) are tracked now, and the sweeps it calls live have ended; `BENCHMARK/REPORT.md` holds the final numbers.

Working-tree state when collected: 12 modified tracked files (`.claude/rules/agent.md`, `.claude/rules/benchmark.md`, `eval/harness/enqueue.py`, `eval/harness/run_manifest.py`, two eval tests, `gateway/models.toml`, `gateway/repolace_gateway/client.py`, `config.py`, three gateway tests) and, as of 2026-10-28, untracked `BENCHMARK/blog-facts.md`, `scripts/visual_report/`, `scripts/wait_for_pro_then_run.sh`, `scripts/wait_for_pro_then_run_roles.sh` (`git status --short`). `docs/INTERVIEW_GUIDE.md` is now tracked (commit `7f4cd9e`); `scripts/sweep_dashboard/` is gone. Line numbers below are from the working tree.

**REFRESH 2026-10-28, sweeps are live.** At 2026-10-28 11:25 UTC the owner wiped and re-enqueued sweep-r2, sweep-e1 and sweep-m1 (`eval/runs/chain.log`: "restart 11:25:34, all three sweeps wiped and re-enqueued"); the old rows were exported to `eval/runs/archive-2026-10-28/{tasks,llm_calls,task_stages,task_test_runs}.csv`. They are running now (e1 started 11:31, r2 11:35, m1 11:50 UTC), so every r2/e1/m1 count in this sheet describes the archived 10-25 incarnation unless marked REFRESH. sweep-r1 is untouched (rows created 2026-10-25 10:49 UTC).

DB times are UTC. Line references use `file:line`.

---

## 1. System facts

### 1a. Pipeline and agent graph

| fact | value | source |
|---|---|---|
| Pipeline stages, in `_stage(...)` call order | instance, preflight, embedder, clone, index, retrieve, verify_baseline, record_baseline, baseline_files, branch, agent, review, merge_target, squash, push, pr, finalize | `pipeline/repolace_pipeline/run.py:779,783,786,799,823,834,858,860,868,878,900,928,955,1021,1033,1035,1061` |
| Agent graph nodes | `localize`, `agent`, `verify` | `agents/repolace_agents/graph.py:409-411` |
| Agent graph edges | START to `localize` to `agent` to `verify`; conditional edge from `verify` via `route` to `agent` or END | `agents/repolace_agents/graph.py:412-415` |
| Graph ends when `state.attempt >= limits.max_attempts` | | `agents/repolace_agents/graph.py:399-400` |
| Recursion limit | `4 * limits.max_attempts + 8` | `agents/repolace_agents/graph.py:108-119` |
| Graph has no `finalize` node; scoring, gate, squash, push, final write are pipeline stages | | `.claude/rules/agent.md:11`; `run.py:1021-1061` |
| Conflict resolver | separate bounded tool loop outside the graph; runs in stage `merge_target` | `agents/repolace_agents/resolver.py`; `run.py:955` |
| Resolver tools (five) and bounds (20 steps, 3 rounds, $0.50 / 40 calls / 600 s) | UNVERIFIED in code; from rules file | `.claude/rules/agent.md:54-56` |

### 1b. The nine tools

| tool | purpose (start of `description=`) | source |
|---|---|---|
| `read_file` | Read a text file from the repository, with line numbers; shows own edits | `agents/repolace_agents/tools/files.py:127-130` |
| `list_dir` | List directory entries; dirs end `/`, symlinks `@`, never followed | `tools/files.py:194-197` |
| `edit_file` | Replace text in an existing file; `old_string` must match exactly and appear once unless `replace_all` | `tools/files.py:262-265` |
| `create_file` | Create a new file; refuses to overwrite; adding test files not allowed | `tools/files.py:356-359` |
| `search_code` | Semantic search over the repository index; returns functions, methods, classes | `tools/search.py:92-95` |
| `grep` | `git grep` over tracked files, POSIX extended regex | `tools/search.py:158-161` |
| `run_python` | Throwaway Python script in the sandbox, no network, repo mounted read-only at `/repo` | `tools/sandbox.py:132-136` |
| `run_tests` | Run some of the repository's tests in the sandbox | `tools/sandbox.py:194-197` |
| `submit` | Ends the loop with a summary; change is then checked against all tests | `tools/toolbox.py:45-49` |
| Max tool calls executed per model reply | 8; extras get `TOO_MANY_CALLS` | `graph.py:82,302-304` |
| Per-tool bounds (400 read lines, 8,000-char result cap, `run_python` timeout 1-120 s, etc.) | UNVERIFIED in code; from rules file | `.claude/rules/agent.md:30-40` |

### 1c. Sandbox containment (`verify/verify/backends/docker.py`)

| flag | value | source |
|---|---|---|
| `--rm`, `--pull=never`, `--init` | set | `docker.py:163,168,171` |
| `--network=none` | unconditional; comment: "there is no spec field to weaken it" | `docker.py:177` |
| `--read-only` | set | `docker.py:178` |
| `--tmpfs=/tmp:rw,nosuid,nodev,size=...` | default size `256m` | `docker.py:184`; `verify/verify/config.py:25` |
| `--cap-drop=ALL` | set | `docker.py:185` |
| `--security-opt=no-new-privileges` | set | `docker.py:186` |
| `--user` | default `65534:65534` | `docker.py:187-188`; `config.py:41` |
| `--memory`, `--memory-swap` | default `2g` each | `docker.py:189-195`; `config.py:14,18` |
| `--cpus` | default `2.0` | `docker.py:196-197`; `config.py:19` |
| `--pids-limit` | default 512 | `docker.py:198-199`; `config.py:23` |
| `--ulimit nofile` | default 4096:4096 | `docker.py:200-201`; `config.py:24` |
| `--log-opt max-size`, `max-file` | set | `docker.py:202-205` |
| `--cpuset-cpus`, `--runtime` | only if configured | `docker.py:208-216` |
| Mounts | `run_python` source and script `:ro`; test-run repo mount `:ro` when `spec.repo_readonly` | `docker.py:218-219,291,378,400` |
| `.git` | not exported into the sandbox (`export_tree` omits it) | `docker.py:17` |
| Flag assertions | `test_docker_argv.py` | `docker.py:155` |

### 1d. No-oracle protocol

| fact | value | source |
|---|---|---|
| Rule | hidden FAIL_TO_PASS tests never reach the agent | `.claude/rules/no-oracle.md:12` |
| Overlay source | `instance.overlay_bytes()` passed as `overlay=` to the verifier | `pipeline/repolace_pipeline/run.py:851` |
| Overlay held read-only | `MappingProxyType(dict(overlay or {}))` | `verify/verify/stage.py:221` |
| `hidden_paths` | `frozenset(self.overlay)` | `stage.py:241-250` |
| Overlay path validation | rejects absolute, `..`, `.git`, NUL/backslash, non-file paths | `stage.py:222`; `verify/verify/overlay.py:24-81` |
| Overlay applied only on scored runs; probes (`run_tests`) carry none | | `stage.py:34,181-186`; `stage.py:184`; `overlay.py:165` |
| Feedback filter drops node ids at or under a hidden path | `overlay_mode` default True, OR-ed with `bool(hidden)` | `agents/repolace_agents/feedback.py:17,275-276,370-384,551-581` |
| Overlay mode keyed on `issue.instance_id is not None` or non-empty `hidden_paths` | | `graph.py:122-129` |
| Pipeline-side probe filter | `visible_probe(result, hidden_paths)` | `pipeline/repolace_pipeline/agent_context.py:120` |
| Edit tools refuse hidden paths | `protected_check` | `agent_context.py:164`; `pipeline/repolace_pipeline/conflicts.py:388` |
| Fixed sentence for unseen tests | "Some tests are not shown to you." (`TESTS_NOT_SHOWN`) | `.claude/rules/no-oracle.md:19`; `tools/sandbox.py:195-197`; `tools/toolbox.py:46-48` |
| Residual risks listed in the rules file | 6, "none is closed"; #5 hidden tests sit in the container's `/repo`, readable by code under test; #6 a timing channel | `.claude/rules/no-oracle.md:22-29` |
| Deferred alternative | feedback from a separate overlay-free run | `.claude/rules/no-oracle.md:31` |
| Tripwire tests named | `agents/tests/test_graph_oracle.py`, `agents/tests/test_feedback.py` (existence UNVERIFIED) | `.claude/rules/no-oracle.md:20` |

### 1e. Default limits

| fact | value | source |
|---|---|---|
| Cost cap per task | $2.00 | `gateway/repolace_gateway/budget.py:42,88` |
| Model-call cap per task | 150 | `budget.py:45,89` |
| Agent clock | 3600 s, starts at the agent stage | `budget.py:50,90`; `run.py:749,899` |
| Attempts | 3 | `agents/repolace_agents/contracts.py:106` |
| Steps per attempt | 40; reaching it gives `StopReason.STEP_CAP` | `contracts.py:107`; `graph.py:259-260` |
| `AgentLimits` ceiling | 200 | `contracts.py:99` |
| Stop reasons | `submitted`, `step_cap`, `budget_usd`, `budget_calls`, `budget_wall`, `llm_error`, `no_change` (and `max_attempts`, UNVERIFIED in code) | `contracts.py:159-173`; `.claude/rules/agent.md:46` |
| Worker per-task bound | 11,700 s | `worker/repolace_worker/config.py:17` |
| Default agent-stage model | `claude-sonnet-5-5` | `gateway/models.toml:22-23` |
| Model used by the reported sweeps | `vertex-gemini-3-1-pro-preview` | `BENCHMARK/REPORT.md:3`; manifests |

### 1f. Counts (run from repo root)

| fact | value | command |
|---|---|---|
| Tracked `.py` files | 297 | `git ls-files '*.py' \| wc -l` |
| Tracked non-`.py` files | 131 | `git ls-files \| grep -v '\.py$' \| wc -l` |
| Python lines, all tracked | 80,676 | `git ls-files '*.py' \| xargs cat \| wc -l` |
| Test files | 133 | `git ls-files '*.py' \| grep -E 'test_[^/]*\.py$' \| wc -l` |
| Test functions (grep) | 3,836 | `... \| xargs grep -h -E '^\s*(async )?def test_' \| wc -l` |
| Tests collected by pytest (parametrized included) | 6,438 in 133 files | `.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider`, per-file counts summed with awk |
| Alembic migrations | 16 (`0001` to `0016`) | `ls api/alembic/versions/*.py \| wc -l` (re-run in this pass: 16) |

Per package. Source: `git ls-files "$d/*.py" \| grep -v '/tests/' \| grep -v 'test_' \| grep -v alembic/versions \| xargs cat \| wc -l`. Tests use `grep -E '/tests/\|/test_[^/]*$'`.

| package | source lines | test lines | test functions |
|---|---|---|---|
| api | 2,421 | 3,550 | 228 |
| worker | 738 | 1,146 | 68 |
| agents | 4,793 | 8,253 | 702 |
| gateway | 2,318 | 3,543 | 285 |
| rag | 1,523 | 1,776 | 172 |
| shared | 4,003 | 5,442 | 422 |
| pipeline | 3,903 | 7,612 | 466 |
| verify | 4,010 | 7,808 | 749 |
| eval | 7,280 | 8,749 | 744 |

`infra`, `scripts`, `ui` not counted (no matching `.py` at the pattern used). `api` source lines include any UI code under `api/`.

---

## 2. Benchmark facts

Source for DB rows: read-only SELECTs on `tasks` joined to `llm_calls` on `task_id`, via `docker exec repolace-postgres-1 psql`. Sweeps are 18 instances x 3 runs = 54 planned rows, except sweep-4b.

### 2a. Headline counts

| sweep | planned | finished | passed | failed | harness errors | still queued | matches REPORT.md |
|---|---|---|---|---|---|---|---|
| sweep-4 | 54 | 42 | 29 | 13 | 12 | 0 | Yes (`REPORT.md:9-10,24`) |
| sweep-4b | 12 | 12 | 6 | 6 | 0 | 0 | Yes (`REPORT.md:11`) |
| sweep-5 | 54 | 31 | 8 | 23 | 3 | 20 | Yes (`REPORT.md:121`: "8 passes of 31 finished") |

| fact | value | source |
|---|---|---|
| Counts re-checked at the end of this pass | identical to the table (e.g. sweep-4: 12 failed, 29 passed, 13 failed-outcome) | `select eval_run_id,status,outcome,count(*) from tasks ... group by 1,2,3` |
| sweep-4 harness errors | all 12: `LLMCallError: provider quota exhausted ... vertex_ai/gemini-3.1-pro-preview` | `select left(error_message,95) from tasks where status='failed'` |
| sweep-5 harness errors | 2 `runner: interrupted`, 1 `runner: abandoned` | same |
| sweep-4 + sweep-4b pooled | 35 / 54 (29 + 6); hand pooling, not tool output | `REPORT.md:12-14` |
| sweep-4 vs `BENCHMARK/data/tasks.csv` | match (29 passed, 13 failed, 12 failed-status) | python Counter over the csv |
| sweep-4 vs `BENCHMARK/data/sweep-4-report.md` | MISMATCH: file shows 11 harness errors, "PARTIAL (53 of 54)", 1 row queued/running, 11 calls with no cost; DB shows 12, 0, 12. Disclosed at `REPORT.md:120`, `BENCHMARK/README.md:10` | `sweep-4-report.md:7-9,13`; DB |
| sweep-4b report header | says "11 instances, 3 runs each"; the manifest has 12 rows (sphinx-9602 twice) and the report flags UNEQUAL RUNS PER INSTANCE | `sweep-4b-report.md:14`; manifest |
| Limits in all three manifests | $2 per task, 150 calls, 40 steps/attempt, 3 attempts, agent wall 3600 s, runner wall 11,700 s; `git_sha` f02ccb3c57be22c4d487776c3fa259457154b1d1 | `BENCHMARK/data/sweep-4*-manifest.json`; `eval/runs/sweep-5/manifest.json` |
| sweep-5 model | `vertex-gemini-3-8-flash` | manifest; `llm_calls.model` |

### 2b. Sweeps r1, r2, e1, m1

REFRESH 2026-10-28: the main repo's `eval/runs` has no such directories, but the manifests exist in the worktrees: `../repolace-roles/eval/runs/{sweep-r1,sweep-r2}/manifest.json`, `../repolace-early/eval/runs/sweep-e1/manifest.json`, `../repolace-mixed/eval/runs/sweep-m1/manifest.json`; the archived 10-25 manifests of r2 and e1 are under `eval/runs/archive-2026-10-28/`. The table below is the 2026-10-26 DB state (archived for r2 and e1); live state follows it.

| sweep | planned | queued | running | failed | completed | completed-passed | completed-failed | any finished |
|---|---|---|---|---|---|---|---|---|
| sweep-r1 | 54 | 44 | 0 | 3 | 7 | 2 | 5 | yes |
| sweep-r2 | 54 | 44 | 0 | 7 | 3 | 0 | 3 | yes |
| sweep-e1 | 54 | 54 | 0 | 0 | 0 | 0 | 0 | no |
| sweep-m1 | 54 | 54 | 0 | 0 | 0 | 0 | 0 | no |

| fact | value | source |
|---|---|---|
| r1 failed rows | 3, all `runner: interrupted` | `error_message` query |
| r2 failed rows | 7: 3 `runner: interrupted`, 3 `provider quota exhausted: plan call`, 1 `provider quota exhausted: agent call` | same |
| r1 and r2 model and stages | `vertex_ai/gemini-3.1-pro-preview`; stages `agent`, `plan`, `review` | `llm_calls` group by stage |
| r1 and r2 are role-split runs | RESOLVED 2026-10-28: manifests have `"roles": true`; r1 `git_sha` 2a57210 (roles-sweep merge of origin/main), r2 `git_sha` 90c0be7 | worktree manifests |
| e1 is the early-edit variant | manifest `git_sha` 6136644 = tip of `exp/early-edit` (the nudge commit); no `roles` key | `../repolace-early/eval/runs/sweep-e1/manifest.json` |
| m1 is the mixed-model roles variant | manifest `git_sha` 196bbfe = tip of `exp/roles-mixed`; `roles: true`, model `vertex-gemini-3-8-flash`, `stage_models` agent 3.8 Flash, `debug`/`plan`/`review` 3.1 Pro | `../repolace-mixed/eval/runs/sweep-m1/manifest.json` |
| Manifests: limits | same as sweep-4: $2, 150 calls, 40 steps, 3 attempts, 3600 s / 11,700 s; 18 instances x 3 | manifests |
| Manifest `created_at` (UTC) | r1 2026-10-25 10:49:18; r2 (live) 2026-10-28 11:25:25, r2 (archived) 2026-10-25 12:19:02; e1 (archived) 2026-10-25 13:31:57, e1 (live) 2026-10-28 11:25:22; m1 2026-10-28 11:25:27 | manifests |
| Archived e1 (10-25 incarnation), from `archive-2026-10-28/tasks.csv` | 13 finished (7 passed, 6 failed), 4 failed, 37 queued; started 2026-10-26 07:10, last completed 2026-10-28 11:24 UTC. The earlier sheet said e1 was all queued; that was true only before 2026-10-26 07:10 | archive csv |
| Archived m1 (10-25 incarnation) | 54 queued, never started | archive csv |
| Live DB 2026-10-28 13:45 UTC (SELECT; moving) | e1: 3 passed, 4 failed, 1 failed-status, 2 running, 44 queued. m1: 2 passed, 6 failed, 2 failed-status, 1 running, 43 queued. r2: 1 passed, 4 failed, 2 failed-status, 47 queued (runner exited 13:27 UTC, `lost429=2`). r1 unchanged | `select eval_run_id,status,outcome,count(*) from tasks ...` |
| Quota or 429 error counts by sweep | sweep-2: 3, sweep-4: 12, sweep-r2: 4 | `select eval_run_id,count(*) from tasks where error_message ilike '%quota%' or '%429%' group by 1` |

### 2c. Model prices (`gateway/models.toml`, USD per million tokens)

| model key | input | output | cache read | cache write | source |
|---|---|---|---|---|---|
| `vertex-gemini-3-1-pro-preview` (sweeps 4, 4b, r1, r2) | 2.00 | 12.00 | 0.20 | 0.0 | `models.toml:182-186` |
| `vertex-gemini-3-8-flash` (sweep-5) | 0.75 | 3.75 | 0.075 | 0.0 | `models.toml:172-176` |
| `claude-sonnet-5-5` (default agent stage; not used by these sweeps) | 2.00 | 10.00 | 0.10 | 2.50 | `models.toml:22-23,45-53` |

| fact | value | source |
|---|---|---|
| 3.1 Pro comment | rates are the AI Studio rates, "CONFIRM THEM AGAINST YOUR CLOUD CONSOLE"; the tier above 200k prompt tokens ($4 / $18) is not modeled | `models.toml:150-159` |
| Calls above 200k input tokens, sweeps 4/4b/5/r1/r2 | 0 | `count(*) filter (where input_tokens>200000)` |
| 3.8 Flash comment | price valid through 2026-12-31; $1.50 / $7.50 from 2027-01-01 | `models.toml:117-121` |
| Provider price pages | not checked. UNVERIFIED | |
| `models.toml` is modified in the working tree | `git status --short` shows ` M gateway/models.toml`; line numbers are the working-tree file | |

### 2d. Spend per sweep

Stored: `SUM(llm_calls.cost_usd)` joined on `task_id`, grouped by `tasks.eval_run_id`. Recomputed from tokens and the 2c rates; the two match to 4 decimals.

| sweep | llm_calls rows | stored sum | calls with NULL cost |
|---|---|---|---|
| sweep-4 | 1611 | $32.3067 | 12 (also NULL tokens) |
| sweep-4b | 420 | $11.8680 | 0 |
| sweep-5 | 1214 | $7.4989 | 4 |
| sweep-r1 | 328 | $7.5723 (agent 5.2925, plan 2.0370, review 0.2428) | 0 |
| sweep-r2 | 152 | $4.1104 (agent 2.5394, plan 1.4306, review 0.1405) | 4 |
| sweep-e1, sweep-m1 | 0 | none | n/a |

sweep-4, sweep-4b and sweep-5 sums were re-run at the end of this pass and match (32.3067 / 11.8680 / 7.4989).

| fact | value | source |
|---|---|---|
| sweep-4 12 failed rows | $3.6351, 146 calls | per-status query |
| sweep-4 42 completed rows | $28.6716, 1465 calls | same |
| sweep-4 + sweep-4b | $44.1747 (`REPORT.md:97`: $44.17) | 32.3067 + 11.8680 |
| Final 54 rows (42 completed + 12 from 4b) | $40.5396 (`REPORT.md:97`: $40.54) | 28.6716 + 11.8680 |
| `tasks.cost_usd` vs `llm_calls` sums | differ: sweep-5 7.1415 vs 7.4989 (23 NULL rows); r1 6.3766 vs 7.5723 (47 NULL); r2 3.7157 vs 4.1104 (49 NULL); sweep-4 32.3066 vs 32.3067 | `select sum(cost_usd), count(*) filter (where cost_usd is null) from tasks` |
| Cause of NULL `tasks.cost_usd` | UNVERIFIED | |

### 2e. Per-instance pass counts

"Final" = sweep-4 completed rows plus all sweep-4b rows. Other columns are finished / passed per sweep.

| instance | final runs | final passes | s4 | s4b | s5 | r1 | r2 |
|---|---|---|---|---|---|---|---|
| mwaskom__seaborn-3069 | 3 | 1 | 2 / 0 | 1 / 1 | 2 / 0 | 1 / 0 | 0 / 0 |
| mwaskom__seaborn-3187 | 3 | 0 | 2 / 0 | 1 / 0 | 2 / 0 | 1 / 0 | 0 / 0 |
| pallets__flask-5014 | 3 | 3 | 2 / 2 | 1 / 1 | 2 / 0 | 1 / 1 | 0 / 0 |
| pylint-dev__pylint-4551 | 3 | 0 | 2 / 0 | 1 / 0 | 2 / 0 | 1 / 0 | 1 / 0 |
| pylint-dev__pylint-4604 | 3 | 0 | 2 / 0 | 1 / 0 | 2 / 0 | 1 / 0 | 1 / 0 |
| pylint-dev__pylint-4970 | 3 | 2 | 2 / 2 | 1 / 0 | 2 / 0 | 1 / 0 | 1 / 0 |
| pytest-dev__pytest-5631 | 3 | 3 | 3 / 3 | 0 / 0 | 2 / 1 | 1 / 1 | 0 / 0 |
| pytest-dev__pytest-6202 | 3 | 3 | 3 / 3 | 0 / 0 | 2 / 2 | 0 / 0 | 0 / 0 |
| pytest-dev__pytest-7236 | 3 | 3 | 3 / 3 | 0 / 0 | 2 / 0 | 0 / 0 | 0 / 0 |
| pytest-dev__pytest-7432 | 3 | 3 | 3 / 3 | 0 / 0 | 2 / 1 | 0 / 0 | 0 / 0 |
| pytest-dev__pytest-7571 | 3 | 3 | 3 / 3 | 0 / 0 | 2 / 0 | 0 / 0 | 0 / 0 |
| pytest-dev__pytest-7982 | 3 | 3 | 3 / 3 | 0 / 0 | 2 / 2 | 0 / 0 | 0 / 0 |
| sphinx-doc__sphinx-7462 | 3 | 1 | 3 / 1 | 0 / 0 | 1 / 0 | 0 / 0 | 0 / 0 |
| sphinx-doc__sphinx-7590 | 3 | 0 | 2 / 0 | 1 / 0 | 2 / 0 | 0 / 0 | 0 / 0 |
| sphinx-doc__sphinx-9230 | 3 | 3 | 2 / 2 | 1 / 1 | 1 / 1 | 0 / 0 | 0 / 0 |
| sphinx-doc__sphinx-9320 | 3 | 3 | 2 / 2 | 1 / 1 | 1 / 1 | 0 / 0 | 0 / 0 |
| sphinx-doc__sphinx-9367 | 3 | 3 | 2 / 2 | 1 / 1 | 1 / 0 | 0 / 0 | 0 / 0 |
| sphinx-doc__sphinx-9602 | 3 | 1 | 1 / 0 | 2 / 1 | 1 / 0 | 0 / 0 | 0 / 0 |
| total | 54 | 35 | 42 / 29 | 12 / 6 | 31 / 8 | 7 / 2 | 3 / 0 |

| fact | value | source |
|---|---|---|
| Final passes per instance | 3 of 3: 10 instances; 2 of 3: 1 (pylint-4970); 1 of 3: 3 (seaborn-3069, sphinx-7462, sphinx-9602); 0 of 3: 4 (seaborn-3187, pylint-4551, pylint-4604, sphinx-7590) | derived from table |
| Matches `REPORT.md` section 3 table (`REPORT.md:34-39`) | yes, instance by instance | comparison |
| Final-outcome query | `row_number() over (partition by instance_id, run_index order by (eval_run_id='sweep-4b') desc)` | query |

### 2f. Figures quoted from REPORT.md (not re-derived)

| fact | value | source |
|---|---|---|
| sweep-4 over finished rows | 29/42 = 69.0% | `REPORT.md:10` |
| Pooled | 35/54 = 64.8% | `REPORT.md:12` |
| Tasks that hit the step cap | 28 of 54 (51.9%) | `REPORT.md:68` |
| Median first-edit call | 9 for passes, 24 for failures | `REPORT.md:76-77` |
| Cost per task, mean / median | $0.751 / $0.645 | `REPORT.md:91` |
| Cost per pass | $1.16 | `REPORT.md:97` |
| Median latency per task | 1,106 s | `REPORT.md:95` |

---

## 3. Episodes

Task ids are `tasks.id`. "Step" is one `llm_calls` row with `stage='agent'`, ordered by `created_at`. Query form: `select created_at, response from llm_calls where task_id='<id>' and stage='agent' order by created_at`. Step-cap flags come from `BENCHMARK/data/tasks.csv` (`stop_reason=step_cap`) and `tool_use.csv`.

### 3a. Step-cap failures (sweep-4, 40 calls, 0 submits)

**S1: `5bf22d90-6c99-4ff8-bfad-aa70ccc555ef`, mwaskom__seaborn-3069.** 4 `edit_file` calls, first at call 19; the `edit_file` at call 28 (`seaborn/_core/plot.py`) set `mode.use_inf_as_na`.

| steps 31-40 (2026-10-23 20:09:06 to 20:11:10) | tool | target |
|---|---|---|
| 31-40 | `run_python` x10 | repro scripts (seaborn.objects / matplotlib, `sharey`, `stripplot`) |

**S2: `1b6e68a0-93b1-4e56-9f88-92d555cb40f8`, mwaskom__seaborn-3187.** 2 `edit_file` calls, first at call 34.

| step | tool | target |
|---|---|---|
| 31-33 | `run_python` | seaborn.objects repro |
| 34 | `edit_file` | `seaborn/_core/scales.py` |
| 35 | `edit_file` | edit whose `new_string` begins with a `# TODO` comment |
| 36-37 | `run_python` | seaborn repro |
| 38-39 | `run_tests` | `tests/test_utils.py` |
| 40 | `run_tests` | `tests/_core/test_scales.py` |

**S3: `8b73193e-7f13-4ebd-b253-913f9e39209f`, pylint-dev__pylint-4604.** 2 `edit_file` calls, first at call 30.

| step | tool | target |
|---|---|---|
| 31-33 | `run_python` | subprocess `python -m pylint` on small snippets (abc/ABC, `xml.etree`) |
| 34 | `run_python` | astroid / `VariablesChecker` inspect |
| 35 | `edit_file` | `is_type_annotation_import` block (20:39:27) |
| 36 | `run_python` | `xml.etree` snippet |
| 37 | `run_tests` | `tests/checkers/unittest_variables.py` |
| 38 | `run_python` | `os.path.exists("tests/checkers/unittest_variables.py")` |
| 39 | `run_tests` | `tests/` |
| 40 | `run_python` | subprocess `pytest tests/checkers/unittest_variables.py` (20:45:01; 20:39:57 to 20:45:01 gap before it) |

### 3b. Passes (sweep-4, `agent_stop_reason=submitted`, `outcome=passed`)

Source: `select patch_diff from tasks where id='<id>'` and `edit_file`/`submit` tool calls in `llm_calls.response`.

**P1: `40e1606a-3032-4b54-b4ca-75892bd090c3`, pytest-dev__pytest-5631.** `score_reason` "all 1 expected tests pass, no regressions"; changed file `src/_pytest/compat.py`; edit 2026-10-23 20:39:58, submit 20:41:13; 15 calls.

```diff
-            [p for p in patchings if not p.attribute_name and p.new in sentinels]
+            [p for p in patchings if not p.attribute_name and any(p.new is s for s in sentinels)]
```

**P2: `5d3946fb-75f8-4541-91b3-a209c40878fd`, pytest-dev__pytest-7982.** Same `score_reason`; changed file `src/_pytest/pathlib.py`; edit 2026-10-23 21:24:33, submit 21:33:33; 33 calls.

```diff
-        if entry.is_dir(follow_symlinks=False) and recurse(entry):
+        if entry.is_dir() and recurse(entry):
```

### 3c. 429 loss

**R1: `fd9dbd53-e832-4190-904f-88de4f0c38ca`, sweep-4, mwaskom__seaborn-3069, run_index 1.** `status=failed`; started 2026-10-23 21:56:06 UTC; error row (`llm_calls`, `stage=agent`) and completion at 2026-10-23 22:23:45 UTC; 31 calls, $0.7378, `scored_attempts=0` (`tasks.csv`).

> agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429): Resource exhausted. Please try again later.

Query: `select error_message, started_at, completed_at from tasks where id='fd9dbd53-...'`; `select created_at,stage,error from llm_calls where task_id='fd9dbd53-...' and error is not null`. The retry window was lengthened later (commit `b661a30`, 2026-10-24 01:17 +0530, "Lengthen the gateway retry window so a Vertex 429 spell does not cost a task").

### 3d. Role-split failure: planner did not submit a plan

**L1: `4dbabbb4-ee19-4a19-96cf-96ea496d0eaf`, sweep-r1, pylint-dev__pylint-4551.** `status=completed`, `outcome=failed`, `agent_stop_reason=no_change`, `score_reason` "agent produced no change", `patch_diff` length 0. Started 2026-10-25 11:33:20; plan calls 11:46:18 to 11:47:02; editor's first call 11:47:06.

| planner calls (stage `plan`) | count |
|---|---|
| `list_dir` | 1 |
| `grep` | 3 |
| `read_file` | 2 |
| `search_code` | 4 |
| `submit_plan` | 0 |

- Last planner call: `search_code {"query": "as_string"}`; the editor's first call follows with no `submit_plan` between. The editor then made 25 calls and produced no diff.
- Across r1: every started task with plan calls has 10 plan calls and 0 `submit_plan`; 4 r1 tasks ended `no_change` (pylint-4551, pylint-4604, pylint-4970, seaborn-3187). r2: `submit_plan` appears on 5 of 10 started rows.
- `agents/repolace_agents/role_prompts.py:159` (roles worktree): "If that last call does not submit, the editor starts without a plan."
- Commit `90c0be7` "Hold back the planner's last step so it always hands over a plan" is dated 2026-10-25 17:48:53 +0530 (12:18 UTC). r1 tasks ran about 10:50-12:14 UTC (DB `started_at`/`completed_at`), before that timestamp. RESOLVED 2026-10-28: the r1 manifest records `git_sha` 2a57210, an ancestor of 90c0be7 (`git merge-base --is-ancestor`), so r1 ran the pre-fix planner. What the workers actually executed is still the manifest's recorded sha, not independently checked.
- Queries: `select ... from llm_calls where task_id='4dbabbb4-...' order by created_at`; counts of `stage='plan'` and `submit_plan` per task for `eval_run_id in ('sweep-r1','sweep-r2')`.

---

## 4. Timeline

Git dates as printed by `git log --date=iso`; commits show both +0530 and +0000. DB times UTC; IST shown where converted.

### 4a. Components first landing

Command: `git log main --reverse --diff-filter=A --format='%h %ad %s' --date=iso -- <dir> | head -1`.

| event | date/time | source |
|---|---|---|
| First commit `2295132` "Scaffold Phase 0 monorepo" (agents, api, eval, gateway, shared, worker, infra first added here) | 2026-08-23 12:43:42 +0530 | `git log --reverse` |
| `rag/` (`2a5a5a3`) | 2026-08-25 01:46:50 +0530 | `-- rag` |
| `pipeline/` (`da72779`) | 2026-08-27 20:00:07 +0530 | `-- pipeline` |
| `verify/` (`8f375d3`) | 2026-08-28 10:52:03 +0530 | `-- verify` |
| `ui/` spec (`0d4be29`) | 2026-10-15 11:07:57 +0530 | `-- ui` |
| Commits reachable from HEAD | 375 | `git log --reverse ... \| wc -l` |

### 4b. Notable commits

Source: `git log --reverse --format='%h %ad %s' --date=iso -i -E --grep='sandbox\|oracle\|benchmark\|tool\|harness\|eval\|sweep\|SWE'` (output truncated by `head -120`; 2026-10-04 to 2026-10-14 not listed), unless noted.

| commit | date (+0530) | subject |
|---|---|---|
| `2df3cc3` | 2026-08-27 19:20:26 | Give the benchmark somewhere to live in the schema |
| `68b75f9` | 2026-08-28 00:11:24 | Decide the Verify sandbox, and record two new product goals |
| `59a0f9e` | 2026-08-28 15:04:22 | Define the scoring rule, after two audits found the old one unsound |
| `55f1717` | 2026-08-30 00:39:45 | Run the repo's suite in a rootless locked-down container (Docker backend first added) |
| `a8d13ed`, `c21bbf6` | 2026-10-01 11:50 | llm_calls and task columns; "Record every model call, scrubbed" |
| `b721f3d` | 2026-10-01 13:47:08 | Define the agent contracts and the toolbox |
| `3b15895` | 2026-10-02 09:07:44 | Implement apply_overlay for hidden benchmark tests |
| `5cef9e0` | 2026-10-02 09:14:20 | Implement the nine agent tools and build_toolbox |
| `510cf30` | 2026-10-02 09:21:12 | Add the no-oracle feedback filter |
| `baee86c` | 2026-10-02 09:21:21 | Wire the agent graph and make run_agent real |
| `aa2e1ab` | 2026-10-03 03:02:29 | Scan the tools for oracle leaks and pin the shared "not shown" sentence |
| `3b7179e` | 2026-10-03 03:10:25 | Add instance selection from SWE-bench Verified, with an auditable manifest |
| `686cb78` | 2026-10-03 08:02:52 | Add the runner: K pipeline subprocesses, a hard timeout and a cost cap |
| `65c5c89` | 2026-10-03 08:07:57 | Add the gold-run analysis that decides which instances can be scored |
| `eb6eab4` | 2026-10-15 19:59:09 | Add the server-rendered web UI |
| `672f6ec` | 2026-10-17 12:50:52 | Add Gemini 3.8 Flash and MiniMax M2.5 as comparison models |
| `0fd8d3a` | 2026-10-17 14:21:25 | Add Gemini on Vertex AI, and stop scoring provider refusals as agent failures |
| `8ef0ad1` | 2026-10-18 00:02:43 | Let a run override the agent's step and attempt bounds, recorded in the manifest |
| `97ca567` | 2026-10-20 23:17:35 | Correct Claude Sonnet 5.5's cache-read price to $0.10 per million tokens |
| `b661a30` | 2026-10-24 01:17:01 | Lengthen the gateway retry window so a Vertex 429 spell does not cost a task |
| `f02ccb3` | 2026-10-24 01:17:31 | Merge PR #50 (`git_sha` in the sweep-4, 4b and 5 manifests) |
| `0e9692d` | 2026-10-26 02:39:11 | Add BENCHMARK: sweep-4 and sweep-4b data, statistical analysis and report (only commit touching `BENCHMARK`) |

### 4c. Eval runs in the DB (UTC)

Query: `select eval_run_id, count(*), min(created_at), max(created_at), min(started_at), max(completed_at), max(completed_at)-min(started_at) from tasks group by 1 order by min(created_at)`. One row has an empty `eval_run_id` (2 tasks, 2026-08-29); its meaning is UNVERIFIED.

| eval_run_id | tasks | first started | last completed | span |
|---|---|---|---|---|
| gold-1 | 18 | 2026-10-04 23:15:01 | 2026-10-05 01:42:04 | 02:27:02 |
| gold-2 | 18 | 2026-10-05 01:55:30 | 2026-10-05 02:13:09 | 00:17:38 |
| gold-3 | 18 | 2026-10-05 04:29:02 | 2026-10-05 05:41:16 | 01:12:14 |
| gold-4 | 18 | 2026-10-05 05:47:19 | 2026-10-05 06:45:45 | 00:58:25 |
| gold-8 | 18 | 2026-10-18 11:24:45 | 2026-10-18 13:48:50 | 02:24:05 |
| gold-9 | 18 | 2026-10-23 16:19:42 | 2026-10-23 18:24:48 | 02:05:06 |
| dry-1 | 3 | 2026-10-17 10:56:08 | 2026-10-17 18:06:56 | 07:10:47 |
| dry-2 | 3 | 2026-10-17 18:33:53 | 2026-10-18 07:27:29 | 12:53:36 |
| sweep-1 | 54 | 2026-10-23 18:25:49 | 2026-10-23 18:29:44 | 00:03:54 |
| sweep-2 | 54 | 2026-10-23 18:55:04 | 2026-10-23 19:33:43 | 00:38:38 |
| sweep-3 | 54 | 2026-10-23 19:48:20 | 2026-10-23 19:48:34 | 00:00:13 |

Also present: gold-pilot, gold-pilot2 to 7, gold-5, gold-6 (2026-10-04 to 10-05), smoke-1 (2026-10-17), dry-3, dry-4 (2026-10-18). Whether the `gold-*`, `dry-*`, `smoke-1` and `sweep-1` to `sweep-3` runs were validation or earlier sweep attempts: UNVERIFIED.

### 4d. Sweeps

Duration = `max(completed_at) - min(started_at)`.

| sweep | tasks | first started (UTC) | last completed (UTC) | duration | IST window |
|---|---|---|---|---|---|
| sweep-4 | 54 | 2026-10-23 19:52:14 | 2026-10-24 14:34:42 | 18:42:28 | 10-24 01:22 to 20:04 |
| sweep-5 | 54 | 2026-10-24 06:57:02 | 2026-10-25 12:23:04 | 1 day 05:26:02 | 10-24 12:27 to 10-25 17:53 |
| sweep-4b | 12 | 2026-10-25 07:09:09 | 2026-10-25 10:59:58 | 03:50:48 | 10-25 12:39 to 16:29 |
| sweep-r1 | 54 | 2026-10-25 10:50:13 | 2026-10-25 12:13:39 | 01:23:26 | 10-25 16:20 to 17:43 |
| sweep-r2 | 54 | 2026-10-25 12:19:19 | 2026-10-25 13:14:19 | 00:54:59 | 10-25 17:49 to 18:44 |
| sweep-e1 | 54 | NULL (all queued) | NULL | n/a | n/a |
| sweep-m1 | 54 | NULL (all queued) | NULL | n/a | n/a |

| fact | value | source |
|---|---|---|
| sweep-4 `llm_calls` window | 2026-10-23 19:52:47 to 2026-10-24 14:34:42 (failed rows include the last call at 14:34:42) | `llm_calls` min/max by sweep and status |
| sweep-5 `llm_calls` last created_at | 2026-10-24 14:20:10, but `tasks.completed_at` maximum is 2026-10-25 12:23:04 (the `interrupted`/`abandoned` rows are 3; link not queried). UNVERIFIED | `llm_calls` and `tasks` min/max |
| Manifest `created_at` vs DB `min(created_at)` | sweep-4 19:51:50 in both; sweep-4b 16:41:37 in both | manifests; DB |
| `BENCHMARK/` file mtimes | `data/*` 2026-10-26 00:28 +0530; `REPORT.md`, `README.md` 00:34:44; `analysis/analyze.py` 00:34:58; `results.json`, `figures/*.svg` 00:35:12 | `ls -la --time-style=full-iso` |
| `BENCHMARK/data` contents | manifests and reports for sweep-4 and sweep-4b, `tasks.csv`, `tool_use.csv`; nothing for sweep-5, r1, r2, e1, m1 | `ls BENCHMARK/data` |

### 4e. Branches relative to main (`git log main..<branch>`, no checkout)

All four share merge-base `f02ccb3c`.

| ref | tip | tip date | ahead of main |
|---|---|---|---|
| `origin/phase-2/agents` | `90c0be7` Hold back the planner's last step so it always hands over a plan | 2026-10-25 17:48:53 +0530 | 13 |
| `roles-sweep` | `90c0be7` (same commit as `origin/phase-2/agents`) | 2026-10-25 17:48:53 +0530 | 13 |
| `exp/roles-mixed` | `196bbfe` Mixed-model roles: debug stage, specific plans for a cheap editor, --stage-model | 2026-10-25 21:51:27 +0530 | 14 (the 13 plus `196bbfe`) |
| `exp/early-edit` | `6136644` Early-edit nudge: ask for a first edit by step 12 and remind once (experiment) | 2026-10-25 19:01:55 +0530 | 1 |

The 13 roles commits, oldest first: `b1b4f22` (2026-10-15) Design the Phase 2 role split behind a roles flag; `35508f1` merge of origin/main; `c32705d` extract `_run_loop`; `f304632` role prompts and tools; `b713cc8` roles flag, off; `ad143ee` roles graph with planner; `85a011c` reviewer; `2f73ad8` debugger; `90f1821` recursion limit by node count; `780a6d5` wire into gateway, CLI, PR body, manifest; `215b5e7` record what the role split is and is not (all 2026-10-17); `2a57210` merge of origin/main (2026-10-25 16:17 +0530); `90c0be7`.

Worktrees: `repolace-early` (exp/early-edit), `repolace-mixed` (exp/roles-mixed), `repolace-roles` (roles-sweep) (`git worktree list`).

---

## 5. Open questions

### 5a. Docs vs data or code

Status as of 2026-10-28 on `main` @ `7f4cd9e`. Commit `7f4cd9e` ("Update docs for the first measured benchmark", 2026-10-26) touched `.claude/rules/benchmark.md`, `.claude/rules/open-questions.md`, `CLAUDE.md`, `README.md`, `docs/PHASE1_TESTING.md` and added `docs/INTERVIEW_GUIDE.md` (1,042 lines).

| item | doc said | status | source |
|---|---|---|---|
| `CLAUDE.md` "Phase 1 status": "Not yet measured: any benchmark number ... cost per task ... any real run against a provider" | stale | RESOLVED in `7f4cd9e`: now states 29/54, 35/54, $0.75 per task, $1.16 per pass, 1,106 s median, 52% step cap; lists retrieval grid, variants and PR opening as still unmeasured | `CLAUDE.md:174` |
| `.claude/rules/benchmark.md` "no sweep has run, so there is no number" | stale | RESOLVED in `7f4cd9e`. The file also carries a further uncommitted working-tree edit (a later line, the `enqueue` bullet) | `.claude/rules/benchmark.md:11` |
| `.claude/rules/open-questions.md` "No sweep has run, so there is no headline number" | stale | RESOLVED in `7f4cd9e`. Its claim that curated ids vs the pytest plugin is "not yet measured" is kept; still UNVERIFIED | `open-questions.md:30` |
| `docs/PHASE1_TESTING.md` "Nothing here has been run against a real task yet"; "the worker cannot run a task as deployed yet" | stale | RESOLVED in `7f4cd9e` (now points to `REPORT.md`; says compose `worker` is written, not run end to end) | `docs/PHASE1_TESTING.md:9,21` |
| `docs/PHASE1_TESTING.md` end: "Put the measured numbers ... into `CLAUDE.md`" | stale | RESOLVED in `7f4cd9e`; the doc now says suite wall times are not yet recorded | `docs/PHASE1_TESTING.md:139` |
| README migrations "0001-0014" | wrong (16) | RESOLVED in `7f4cd9e` ("0001-0016") | `README.md:16` |
| README agent row "never run against a real provider" | stale | RESOLVED in `7f4cd9e` | `README.md:22` |
| README benchmark harness row "Built, not yet run" | stale | RESOLVED in `7f4cd9e` | `README.md:24` |
| README compose worker row "no maintenance worker or beat, no `env_file`, no docker socket" | wrong | RESOLVED in `7f4cd9e` (row now describes `worker` and `worker-maintenance`, checked with `docker compose config` only) | `README.md:25` |
| README "What comes next" lists Phase 1 completion as undone | stale | RESOLVED in `7f4cd9e` | `README.md:277` |
| README variants "none merged to `main`"; role split on `phase-2/agents` | agreed with 4e | still agrees | `README.md:208` |
| README retry-loop row "Built and wired in, not yet exercised by a real run" | possibly stale | STILL OPEN: whether the retry loop was exercised in the real runs is UNVERIFIED (not checked in this pass) | `README.md:23` |
| README worker paragraph "minimal today (see the status table)" | stale | STILL OPEN: contradicts the status-table row now at `README.md:25` | `README.md:56` |
| `CLAUDE.md` `asyncio.timeout(...)` in "What exists now" | stale | RESOLVED in `7f4cd9e` (now `repolace_worker.bound.bounded` / `WorkerTimeout`) | `CLAUDE.md:124`, `160` |
| Code comments still say `asyncio.timeout` as the primary limit | stale comments | STILL OPEN (code, not docs; `7f4cd9e` did not touch them) | `worker/repolace_worker/celery_app.py:34`; `worker/repolace_worker/config.py:29` |
| `CLAUDE.md` pipeline description "(retrieve -> plan -> edit -> test -> review)" | main has `localize -> agent -> verify`; planner and reviewer exist only on the role-split branch (1a, 4e) | STILL OPEN: new in this refresh; `7f4cd9e` did not touch it. Quote with care | `CLAUDE.md:7` |
| `CLAUDE.md` "The 32 Docker containment tests have passed on the rootless daemon" | see 5c | kept by `7f4cd9e`; count VERIFIED, pass result UNVERIFIED | `CLAUDE.md:174` |
| `sweep-4-report.md` 11 harness errors, 53 of 54 | DB: 12, 54 of 54 | STILL OPEN as a data file (disclosed in `REPORT.md:120`; guide section 13b also discloses it) | see 2a |
| `sweep-4b-report.md` header "11 instances ... 3 runs each" | 12 rows over 11 instances | STILL OPEN (disclosed in guide 13b) | see 2a |
| `REPORT.md:24` 429s are why rows were lost | sweep-4: yes (12). Other sweeps lost rows to `runner: interrupted` too | STILL OPEN as a scope note: true for sweep-4 only | 2b |
| `models.toml` sets `claude-sonnet-5-5` for the agent stage | every benchmark sweep used Vertex Gemini models | STILL OPEN but now stated openly in `CLAUDE.md:174` and README ("the shipped `agent` stage is still `claude-sonnet-5-5`") | `models.toml:22-23`; `llm_calls.model` |
| Cost columns: `tasks.cost_usd` NULL on many rows in sweeps 5, r1, r2 | sums differ from `llm_calls` | STILL OPEN (data; cause UNVERIFIED) | 2d |
| Vertex price: docs say "taken from AI Studio, to be confirmed" | unconfirmed | PARTLY RESOLVED 2026-10-28: published Vertex rates match `models.toml`, see 5c. The warning comment at `models.toml:150-159` and `CLAUDE.md:174` is still there | `models.toml:150-159` |

### 5b. Not established

| item | detail |
|---|---|
| Branch behind sweep-r1, r2, e1, m1 | RESOLVED 2026-10-28 from worktree manifests (2b): roles-sweep, roles-sweep, exp/early-edit, exp/roles-mixed, by `git_sha` and `roles` key. No DB branch column |
| Why r1, r2 and sweep-5 stopped with queued rows | partly: `eval/runs/chain.log` and runner logs show r1 ended in `KeyboardInterrupt` (manual stop); r2 runner (10-28) exited with `lost429=2`; reason for sweep-5 not found |
| Whether sweep-5 and r1 were stopped by hand | DB shows timestamps and `runner: interrupted/abandoned` messages only |
| Empty `eval_run_id` row | meaning not determined |
| Model, limits and `git_sha` for r1, r2, e1, m1 | RESOLVED, see 2b (worktree manifests) |
| Commits 2026-10-04 to 2026-10-14 | not listed in the truncated grep |

### 5c. Checks added 2026-10-28

| item | result | source |
|---|---|---|
| "32 Docker containment tests" | `pytest -m docker --collect-only` (collect only, nothing run, no containers started) selects exactly 32 tests, all in `verify/tests/test_docker_backend.py`. The count is VERIFIED. The claim "have passed on the rootless daemon" has no recorded result: no junit file, no log; `.pytest_cache/v/cache/lastfailed` (2026-10-18) has no `test_docker_backend` entry and `nodeids` lists them, which shows only that they were collected. Pass state UNVERIFIED. The sentence has been in `CLAUDE.md` since commit `b466919` (2026-10-05) | `.venv/bin/python -m pytest -m docker --collect-only -q`; `conftest.py:133` (marker skips when no daemon, fails if `REPOLACE_TEST_DOCKER_REQUIRED=1`) |
| Vertex price for `gemini-3.1-pro-preview` | Google Cloud pricing page (via search summary; the page itself is JavaScript-rendered and WebFetch returned only its title): global endpoint, up to 200K prompt tokens, $2.00 input, $0.20 cached input, $12.00 text output "including reasoning"; above 200K: $4.00 / $0.40 / $18.00. Vertex thinking docs: thinking tokens are billed and counted as output. These equal `models.toml` (2.00 / 12.00 / 0.20) and AI Studio. Not checked: whether a regional endpoint carries a premium; which endpoint the gateway calls | https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing ; https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thinking |
| Cost recompute from tokens | sweep-4: 1,611 calls, 33,070,388 input (22,143,968 cached), 502,087 output tokens; sweep-4b: 420 calls, 10,038,659 input (5,501,652 cached), 141,135 output. Recompute `(input - cached) x 2 + cached x 0.2 + output x 12` per M gives $32.3067 and $11.8680, equal to stored. 0 calls above 200K input. The figures $44.17, $40.54, $0.75 per task and $1.16 per pass therefore stand, assuming `input_tokens` includes cached tokens (inferred, matches to 4 decimals) | `llm_calls` SELECT |

### 5d. `docs/INTERVIEW_GUIDE.md` vs sections 1-4 of this sheet

The guide (Updated 2026-10-25, checked against `f02ccb3`) was written by an assistant session and says sections 1-12 and 14 were not re-read since 2026-10-19. Agreements (not listed individually): headline 29/54, 35/54, 29/42, per-instance passes, $32.31 / $11.87 spend, $0.75 / $0.67 per task, 1,106 s median, step-cap 28 of 54, caps ($2, 150, 40, 3), manifest sha f02ccb3, 16 migrations, about 3,800 test functions (sheet 3,836), sweep-5 8 / 23 / 3 / 20, r1 7 finished (2 passed) plus 3 `runner: interrupted`, r2 3 finished plus 7 failed.

| item | guide says | sheet says | where |
|---|---|---|---|
| Package line counts | shared 4.2k, verify 4.1k, agents 5.3k, gateway 2.7k, rag 1.6k, pipeline 4.7k, worker 0.8k, api 3.7k, eval 6.4k; "about 33k" non-test lines | 4,003 / 4,010 / 4,793 / 2,318 / 1,523 / 3,903 / 738 / 2,421 / 7,280; total 30,989. Different method and date (guide read 2026-10-19); pipeline, api and agents differ by 0.5-1.3k, eval by 0.9k the other way | guide `:85-93`; 1f |
| `sweep-e1` | "no `sweep-e1` row in the database yet" | 54 rows existed by 2026-10-26; archived run had 13 finished; live again since 2026-10-28 | guide 13b "Experiment in progress"; 2b |
| `sweep-r2` planners | "planners submitted 5 of 5" | `submit_plan` on 5 of 10 started rows (archived data); the 5 of 5 probably counts only planners that finished. Live r2 (10-28 rerun, 13:45 UTC): `submit_plan` in 6 of 7 tasks with plan calls (r1: 0 of 9) | guide 13b; 3d |
| r1 stop reasons | "of 7 scored tasks 4 `no_change`, 3 `step_cap`" | 4 `no_change` confirmed (3d); the `step_cap` count of 3 not re-queried | guide 13b; 3d |
| "Stopped after 10 of 54 rows" (r1) | 10 started | 10 rows non-queued (7 completed + 3 failed). Agrees | 2b |
| sweep-4 interval | official "29 of 54 = 54%, interval 33%-72%" | `CLAUDE.md:174` says 35 to 72%; the sheet quotes no interval. `REPORT.md` table at line 7 was not re-read here. Three docs, two lower bounds: UNVERIFIED which is right | guide 13b; `CLAUDE.md:174` |
| r1 report location | `../repolace-roles/eval/runs/sweep-r1/report.md` | exists (written 2026-10-25 17:50 IST); the earlier sheet said no r1 files exist | 2b |
| Headline pooling interval 44%-83% | stated as hand-resampled | `CLAUDE.md:174` and `REPORT.md` agree (44 to 83) | agree |
| Doc date | `main` at `f02ccb3` plus uncommitted work | sheet was at `0e9692d`, now `7f4cd9e`; guide itself is committed in `7f4cd9e` | header |
| `dry-4` $0.56 / 15 min / 80 steps; chunk counts 20,629; "about 65% cached" and 34.9 calls per task | not in the sheet | not checked here. Cached share check: sweep-4 22.1M of 33.1M = 67%; sweep-4b 5.5M of 10.0M = 55% | guide 13b; `llm_calls` |

---

## UNVERIFIED

- Per-tool bounds (read 400 lines, 8,000-char result cap, edit/create sizes, `run_python` and `run_tests` timeouts, `grep` limits): from `.claude/rules/agent.md:28-42`, not read in code.
- Resolver five-tool list and bounds: from `.claude/rules/agent.md:54-56`.
- Eighth stop reason `max_attempts`: from `.claude/rules/agent.md:46`; enum value not read.
- "32 Docker containment tests" (`CLAUDE.md:174`): count of 32 verified by collect-only (5c); pass state UNVERIFIED, tests not run.
- Tripwire test files named in `no-oracle.md:20`: existence and pass state not checked.
- `hints_text` absent from `IssueContext`: from rules file.
- Every figure in 2f and every statistic in `REPORT.md` sections 3-8 other than those re-derived in 2a, 2d and 2e (no `analyze.py` run, CSVs not opened except the tasks.csv counts in 2a).
- Model prices against provider pages: 3.1 Pro checked against Google's pricing page via search summary only (5c); regional-endpoint premium and the endpoint used UNVERIFIED. 3.8 Flash and Sonnet 5.5 not checked.
- That the workers really executed the manifest's `git_sha` (the manifests are the only evidence for r1, r2, e1, m1 code).
- The planner cap of about 10 steps and the editor cap of about 25 calls are inferred from DB counts.
- Cause of NULL `tasks.cost_usd` and of the sweep-5 `completed_at` span.
- Whether the retry loop was exercised in the real runs.
- Whether curated fail-to-pass ids match the pytest plugin's output.
- Whether the compose worker stack runs.
- Whether `input_tokens` includes cached tokens (inferred from the cost recompute).
- `ui/` line counts; `infra` and `scripts` line counts.
- Contents of the untracked `scripts/visual_report/`, `scripts/wait_for_pro_then_run*.sh`, and of the modified files other than the rules-file diffs noted.
- `.claude/rules/agent.md:11` claim of the default LangGraph recursion limit "10007".
- `eval/runs/retrieval-1`: seen in a directory listing only, not opened.
