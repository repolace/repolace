# Phase 2: splitting the agent into roles

Lives on `phase-2/agents` and is **not merged** until the Phase 1 benchmark has run on `main`.
Only the role split is here; every other Phase 2 item (worker, queue, UI, merge-conflict handling
and its pipeline hooks) is ordinary work on `main`.

The split exists to answer one question with the same instances: does it beat the single agent?
So `roles=False` must reproduce today's graph byte for byte.

## The contract that does not change
- `AgentRunner`: `async (AgentDeps) -> AgentResult` (`contracts.py`). `AgentDeps`, `AgentResult` and
  the eight `StopReason` values stay as they are; the stop reasons are mirrored behind
  `ck_tasks_agent_stop_reason`, so a new one needs a migration. Map role failures onto the existing eight.
- `AgentResult.attempts` counts only `verify_attempt` records. `run._check_against_scorer` fails the
  task if it disagrees with the scorer, and the review stage rewinds to `last_attempt.commit_sha`.
- The pipeline calls `run_agent`, never `run_graph`. Roles inherit the task's `task_scope` and budget
  (150 calls, $2, 3600 s) and cannot bypass it.

## Graph with roles on
`localize -> plan -> edit -> verify -> route`, where `route` sends a red result to `debug` (then back to
`verify`) and a clean one to `review`; a `request_changes` verdict becomes one more edit attempt, within
`max_attempts`. With `roles=False`, `build_graph` returns the current `localize -> agent -> verify -> route`.

| role | tools | notes |
|---|---|---|
| planner | `search_code`, `read_file`, `grep`, `list_dir`, `submit_plan` | read-only; its plan reaches the editor framed as `data_block("plan", nonce, ...)`, because it is derived from untrusted issue text |
| editor | all nine | today's agent, with the plan in its first message |
| debugger | editor tools plus `rewind(attempt)` | feedback only from `render_feedback`; `rewind` accepts only scorer-recorded commits |
| reviewer | read-only plus `submit_review` | reads `review_diff` against the plan as a data block; its verdict can add a retry, never read the oracle |

## Implementation steps
1. Extract the inlined tool loop in `graph.py:245-330` into `_run_loop(ctx, messages, toolbox, stage, step_cap, exit_tool)`; the current `agent` node becomes its first caller with no behaviour change (tests stay green).
2. `ToolBox.subset(names)` and per-role schemas; the system prompt becomes a pure function of `(role, limits, nonce)`, and the "hostile issue leaves the prompt identical" test runs per role.
3. `roles: bool = False` as a runner/`AgentLimits` option, **not** a field of `AgentDeps`.
4. Planner, then reviewer, then debugger, each with tests; add `plan` to the closer-neutralised tag families in `render.py`.
5. Raise `recursion_limit_for` with the node count.

## Invariants the split must keep
- `verify` is the only node that reads `deps.baseline` or an attempt's `result`. Add every new module to `ORACLE_SCANNED_FILES` and `ORACLE_BLIND_FILES` in `tests/test_graph_oracle.py`, and extend the metamorphic and graph-differential tests so role routing is not a function of hidden outcomes.
- Each role loop gets its own step cap, splitting the 40 steps per attempt rather than multiplying them; the gateway's call cap still bounds the total.

## Gateway and reporting
- `gateway/models.toml` has only the `agent` and `cheap` stages (both required). `cheap` has `max_tokens=1024` and no cache, which a tool-loop planner cannot use; add `plan` and `review` stages. `llm_calls.stage` is a free string, so no migration.
- For a sweep, new stages must default to the agent's model, or the comparison mixes models. The eval manifest sets only `{"agent": model}` today.
- `finalize.first_model` takes the earliest call, so a planner on a cheaper model would make the PR body and report name that model; pick the editor's.
- The CLI preflights only the agent stage (`cli.py`); preflight every stage the roles use, or a missing key fails mid-run.
- Record `roles` in the run manifest (`eval/harness/run_manifest.py`) and pass it through enqueue and the runner.

## Open question
The merge-conflict resolver is not part of this branch. If it needs a tool set or graph node inside `agents/`, decide then which side it lands on.
