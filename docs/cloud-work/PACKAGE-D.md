# Package D: the agent role split, on the long-lived branch `phase-2/agents`

Read `COMMON.md` first. Budget: **hard stop at about $28**. Model: Claude Opus 5.5.
Branch: **`phase-2/agents`** (it already exists on the remote with one commit, `agents/PHASE2.md`).
**Do not open a pull request into `main`, and do not merge it there.** This branch stays unmerged until the
Phase 1 benchmark has run on `main`, so the benchmark is measured on the code that was benchmarked; only then
is the split compared against it on the same instances (a decision for the user, later). Push your commits to
`phase-2/agents` itself. This is the one place in these packages where "never push to `main`" is joined by
"never open a PR to `main`".

## Goal

Implement the design already written in `agents/PHASE2.md` (on this branch): split the single tool-loop agent
into planner, editor, debugger and reviewer roles behind a `roles` flag, with `roles=False` reproducing today's
graph **exactly**. The split exists to answer one question with the same instances: does it beat the single
agent? Everything below follows from keeping that comparison honest.

## First

1. `git switch phase-2/agents && git merge origin/main`. Resolve conflicts in favour of `main` for everything
   outside `agents/`. Run the full checks from `COMMON.md`; they must be green **before** you change anything,
   and again after the merge commit. Never rebase or force-push this branch; merge.
2. Read, in this order: `agents/PHASE2.md`, `.claude/rules/agent.md`, `.claude/rules/no-oracle.md`,
   `.claude/rules/prompt-injection.md`, then `agents/repolace_agents/graph.py` and `tools/`.

## Do, in the design's own order (each step green before the next)

1. Extract the inlined tool loop in `graph.py` into `_run_loop(ctx, messages, toolbox, stage, step_cap,
   exit_tool)`. The existing `agent` node becomes its first caller with **no behaviour change**; the whole
   existing `agents` and `pipeline` suites stay green and unmodified. Commit this alone.
2. `ToolBox.subset(names)` and per-role tool schemas. The system prompt becomes a pure function of
   `(role, limits, nonce)`; the existing "a hostile issue leaves the prompt identical" test runs once per role.
3. `roles: bool = False` as a runner/`AgentLimits` option, **not** a field of `AgentDeps`. With it false,
   `build_graph` returns the current `localize -> agent -> verify -> route` and a differential test proves the
   transcript, stop reason and spend are identical to before.
4. Planner, then reviewer, then debugger, each with tests. Plan and review text reach the next role framed with
   the existing nonce-delimited data blocks (`data_block("plan", nonce, ...)`); add `plan` to the closer-neutralised
   tag families in `render.py`, with the same adversarial tests as the other families.
5. Raise `recursion_limit_for` with the node count, with a test that a routing bug still ends in a
   `GraphRecursionError` within a bounded number of steps.
6. Gateway and reporting, as the design lists them: `plan` and `review` stages in `gateway/models.toml` that
   **default to the agent's model** (the existing `OPTIONAL_STAGE_FALLBACKS` mechanism: a sweep must never mix
   models by accident); the CLI/worker preflight covers every stage the roles use; `finalize.first_model` names
   the editor's model, not the planner's; the run manifest records `roles` and the eval enqueue/runner pass it
   through (`eval/harness/run_manifest.py`). These edits outside `agents/` are the **only** ones this package may
   make, and only on this branch.

## Invariants (a violation is a failed package, not a nit)

* **The no-oracle protocol.** `verify` stays the only node that reads `deps.baseline` or an attempt's `result`.
  Add every new module to `ORACLE_SCANNED_FILES` and `ORACLE_BLIND_FILES` in `agents/tests/test_graph_oracle.py`
  and extend the metamorphic and graph-differential tests so role routing is **not** a function of hidden-test
  outcomes. The reviewer can add an edit attempt; it can never read the oracle.
* **The contract does not change.** `AgentRunner`, `AgentDeps`, `AgentResult` and the **eight** `StopReason`
  values stay as they are (they are mirrored behind a DB CHECK constraint; a ninth needs a migration, which you
  must not add). Map role failures onto the existing eight. `AgentResult.attempts` counts only `verify_attempt`
  records; `run._check_against_scorer` must keep passing.
* **Budgets are shared, not multiplied.** Roles inherit the task's `task_scope` and budget (150 calls, $2,
  3600 s). Each role loop gets its own step cap that *splits* the 40 steps per attempt.
* **The pipeline calls `run_agent`, never `run_graph`.** Keep it that way.
* **Prompt-injection posture is unchanged**: untrusted text is only ever inside the existing framed blocks; the
  planner and reviewer are read-only; the debugger's `rewind` accepts only scorer-recorded commits.

## Acceptance

* With `roles=False` every existing test passes unmodified and the differential test proves identical behaviour.
* With `roles=True`, scripted-fake-model tests (no real model) cover: a plan reaches the editor framed; a red
  attempt routes to the debugger; a clean one to the reviewer; a `request_changes` verdict adds exactly one more
  attempt within `max_attempts`; every role respects its step cap and the shared budget.
* The full `agents`, `pipeline`, `gateway`, `eval/tests` suites are green on the branch.
* `agents/PHASE2.md` is updated from "design" to "what exists and what does not", and `.claude/rules/agent.md`
  gains a short section stating that roles are optional, off by default and unbenchmarked.

## Must not touch

`verify/`, `shared/` (no schema or model changes), `api/`, `worker/`, `infra/`, and anything on `main` that is
not part of the six steps above. No migrations. Do not enable `roles` by default anywhere. Do not merge into
`main` or open a PR into it. Leave `pipeline/repolace_pipeline/run.py` alone except for the preflight/manifest
plumbing in step 6, because package C owns it on `main`.
