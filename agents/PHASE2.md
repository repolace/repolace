# Phase 2: the agent split into roles

Lives on `phase-2/agents` and is **not merged** until the Phase 1 benchmark has run on `main`.
Only the role split is here; every other Phase 2 item (worker, queue, UI, merge-conflict handling
and its pipeline hooks) is ordinary work on `main`.

The split exists to answer one question with the same instances: does it beat the single agent?
So it is **off by default everywhere**, and with it off the graph is the single agent's, byte for
byte. Nothing below has run against a real model, so nothing here says the split helps.

## What exists

**The switch.** `AgentLimits.roles: bool = False`. The pipeline and the worker build
`AgentLimits()`, so they run the single agent. Only the runner turns it on: `LLMAgent(roles=True)`
replaces `deps.limits` with `roles=True` and still calls `run_agent`. `repolace-run-task --roles`
(only with `--agent llm`) builds that runner; `repolace-eval enqueue --roles` records it in the run
manifest, and `repolace-eval run --roles` passes it to every task and refuses to start if the
manifest disagrees. `AgentDeps`, `AgentResult`, `AgentRunner` and the eight `StopReason` values are
unchanged (no migration).

**The graph** (`repolace_agents/roles.py`, built by `graph.build_graph(roles=True)`):

```
localize -> planner -> editor -> verify -> route_roles
               |                            |-- red, attempts left   -> debugger -> verify
               `-- budget / LLM stop: END   |-- clean, attempts left -> reviewer -> editor | END
                                            `-- anything else        -> END
```

| role | stage | tools | first input |
|---|---|---|---|
| planner | `plan` | `search_code`, `read_file`, `grep`, `list_dir`, `submit_plan` | the briefing: the issue, repository and retrieval blocks of the single agent's localize message plus the filtered baseline summary |
| editor | `agent` | the nine | the briefing and the plan in a `plan` block (or "no plan") |
| debugger | `agent` | the nine plus `rewind(attempt)` | the editor's conversation, earlier tool outputs elided, ending in the feedback filter's message |
| reviewer | `review` | the four read-only tools plus `submit_review` | the briefing, the plan, and `git diff <base>...HEAD` in an `output` block |

- Every model-calling node goes through `graph._run_loop`, the single agent's loop extracted
  unchanged, so every bound holds for every role: 8 tool calls per reply, the budget checked
  between calls, mid-attempt elision, malformed arguments replayed as `{}`, every call answered.
- `verify` is the single agent's node, still the only reader of `deps.baseline` and of a result,
  wrapped only to remember `(attempt, commit_sha)` for `rewind`. `route_roles` reads exactly what
  `route` reads. So hidden tests steer a roles run by the same one bit as the single agent.
- The editor and the debugger share one transcript (the single agent's conversation, the system
  message swapped to the role running); the planner and the reviewer each get a fresh one.
- A request for changes adds exactly one editing attempt within `max_attempts`, with the comments
  in a `review` block; the last attempt is never reviewed.
- `rewind` takes a number and resolves it only against commits the scorer recorded, through
  `GitRepo.reset_hard`. The reviewer's diff is `GitRepo.diff_from(base)`, with `base` the HEAD read
  once before the planner. Both are the calls the pipeline's `rewind_to` and `review_diff` make.
- Step caps split the 40 steps per attempt: planner a quarter, reviewer an eighth, editor and
  debugger the rest (10/5/25); at least 3 steps per attempt. One gateway `task_scope` and budget
  for all roles. Recursion limit `6 * max_attempts + 10`.
- Role failures map onto the eight stop reasons: a budget or provider failure in the planner or the
  reviewer is that `BUDGET_*` or `LLM_ERROR` (a provider refusal still propagates); a planner that
  never submits leaves the editor without a plan; a reviewer with no verdict leaves the change
  standing; an editor that changes nothing after a review keeps its own stop reason.
- **Planner hand-over.** The first sweep of the split (`sweep-r1`, stopped at 10 of 54 rows) showed the planner exploring to its 10-step cap and never calling `submit_plan` (0 of 7), so 4 of 7 tasks ended `no_change` with the editor starting cold and 25 steps. `roles.planner` now explores for `plan - 1` steps and, if it has not submitted, spends its last step on a hand-over call that offers only `submit_plan`. The step split (10/25/5) and every other prompt are unchanged, so `sweep-r2` isolates that one change.
- Prompts (`role_prompts.py`): each system prompt is a pure function of `(role, limits, nonce)`;
  `Role.AGENT` is the single agent's prompt unchanged. Role tools are in `tools/role_tools.py`.
- `render.py` neutralises `plan` and `review` closers along with the six other families.

**Gateway and reporting.** `plan` and `review` are optional stages in
`OPTIONAL_STAGE_FALLBACKS`, deliberately without a table in `gateway/models.toml`, so they follow
`agent` (and a sweep's `--model`) unless a config names them. With `--roles` the CLI preflights
`agent`, `resolve`, `plan` and `review`; without it, `agent` and `resolve` as before.
`finalize.first_model` names the first `agent`-stage call, the editor's. The manifest's `roles` key is
written only when true, so a single-agent manifest is unchanged.

**Tests.** `test_roles_differential.py` (sixteen scenarios through `run_graph` and through a verbatim
copy of the pre-split agent node: identical requests, results and spend), `test_roles_graph.py`
(routing, caps, budget, per-role hostile issue, recursion guard), `test_roles_oracle.py` (sentinels
and hidden-only differences under the roles), `test_roles_rewind.py` (real git),
`test_roles_prompts.py`; `pipeline/tests/test_pipeline_roles*.py` (CLI, preflight, and a real
`run_task` through the roles with real git and the real gateway client over a scripted provider);
`gateway/tests/test_gateway_role_stages.py`; `eval/tests/test_eval_roles.py`.

## What does not exist

- **Any measurement.** No roles run against a real provider, no benchmark comparison. The step-cap
  proportions are a starting point, not a measured optimum.
- **Roles in the worker.** The Celery worker always runs the single agent; nothing there sets
  `roles`. Its preflight therefore stays `agent` and `resolve`.
- **The report naming the editor's model.** `eval/harness/report.py` takes a task's first call of any
  stage as its model; with the default config every role uses one model, so this only matters for a
  config that points `plan` elsewhere. Not changed here (the report belongs to the Phase 1 benchmark).
- **The merge-conflict resolver** is not part of this branch and does not share the roles' graph.
