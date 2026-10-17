# Package C: wire the merge-conflict resolver into the pipeline

Read `COMMON.md` first. Budget: **hard stop at about $20**. Model: Claude Opus 5.5.
Branch: `cloud/c-conflict-resolver`, from `main`. PR target: `main`.
**Migration number:** you need one (a nullable `tasks.conflict_reason`). Package B takes `0015` and will usually
merge first. Write yours as `0015` on top of `0014` while you work, and **renumber to `0016` with
`down_revision = "0015"` when you rebase onto a `main` that has B's migration.** Say in the PR which you did.

## Goal

If the target branch moved while a task ran, the pull request conflicts. Everything needed to handle that is
built and tested in isolation and **nothing calls it**:

* git primitives on `GitRepo` and `TaskWorkspace` (`shared/repolace_shared/git/repo.py`, `workspace.py`):
  `fetch_branch`, `merge_tree`, `merge_no_commit`, `abort_merge`, `unmerged_files`, `conflict_kind`,
  `TaskWorkspace.target_moved()`, `advance_base(sha)`, `commit_resolution(paths, message)`.
  `.claude/rules/git-and-pr-flow.md` ("Primitives for a target branch that moved after the clone") explains
  their safety checks; read it.
* the resolver agent, `resolve_conflicts(ResolverDeps) -> ResolverResult` in
  `agents/repolace_agents/resolver.py` (five tools, three rounds, a host-side check after each submit).
  `.claude/rules/agent.md` ("Conflict resolver") is its contract.

Your job is the pipeline code that calls them in the right order and handles every outcome honestly.

## The flow (decided; follow it)

Add the step in `pipeline/repolace_pipeline/run.py` after the agent stage and its scoring, **before** the
review/squash/push stages, as a new stage (record it in `task_stages` like the others; see how `_stage` works).
Put the logic in a new module `pipeline/repolace_pipeline/conflicts.py` so `run.py` only calls it.

1. `target_moved()`. If not moved: nothing changes (this must stay the common path, byte for byte).
2. Moved: `fetch_branch`, then `merge_tree` to learn whether it merges cleanly without touching the tree.
3. **Clean merge:** `merge_no_commit`, commit it (`commit_merge`), `advance_base(new_sha)`.
4. **Conflicts, all plain text** (`conflict_kind` says both-modified or both-added; nothing deleted, renamed,
   mode-changed, a symlink or a gitlink): `merge_no_commit`, call `resolve_conflicts` with a `ResolverDeps`
   whose `check` is the host's re-check (no marker lines, no unmerged entries) and whose `is_protected` is the
   **same baseline-aware closure the main agent gets** (`pipeline/repolace_pipeline/agent_context.py::protected_check`).
   On `RESOLVED`, `commit_resolution(paths, message)`, then `advance_base`.
5. **After any merge, the old score no longer describes the tree.** Run a new scored attempt through
   `AttemptScorer.verify_attempt` (`pipeline/repolace_pipeline/attempts.py`) so the suite is run on the merged
   result, and judge that attempt; the PR gate and the outcome use it, not the pre-merge one. The pre-merge
   attempt stays in `task_test_runs`. This costs one more suite run; that is the price of an honest score.
6. **Cannot resolve** (a non-plain-text conflict, `GAVE_UP`, `ROUNDS_EXHAUSTED`, `STEP_CAP`, `BUDGET`,
   `LLM_ERROR`, `PROTECTED_PATH`): `abort_merge`, end the task with status **`conflicting`** (it exists in
   `TaskStatus` and nothing sets it), open **no PR**, keep the outcome the pre-merge attempt scored, and store a
   short non-secret reason in the new `tasks.conflict_reason`. `error_message` stays NULL: it means only that
   repolace broke. A resolver that raised because the provider refused us (`LLMCallError.provider_refused`) is a
   harness failure like anywhere else: let it propagate.
7. The resolver gets its **own** `TaskBudget` inside the surrounding `task_scope` (limits in
   `agents/repolace_agents/resolver.py`; the call, cost and wall limits are the gateway's). Use half of the
   agent's remaining cost and wall budget, never more than $0.50 and 600 s.
8. **Preflight**: the CLI (`pipeline/repolace_pipeline/cli.py`) and the worker preflight must cover both the
   `agent` and `resolve` stages (`resources.py`, `build_resources`). `resolve` follows `agent` through
   `OPTIONAL_STAGE_FALLBACKS`; a missing key or price must be exit code 2 before any task is claimed.
   Check what already exists before adding anything.

## Tests (temp git repositories; no network, no Docker)

* target not moved: the new stage is a no-op and the task is unchanged.
* clean merge: base advanced, a new scored attempt exists, the PR gate uses it.
* one both-modified conflict resolved by a scripted fake resolver: marker-free, committed, base advanced.
* resolver gives up; a delete/modify conflict; a symlink conflict: `conflicting`, no PR, `abort_merge` leaves
  the checkout at its pre-merge commit, `conflict_reason` set, `error_message` NULL.
* the resolver is never handed a path outside the conflicted set; `.git` is never writable.
* `provider_refused` propagates and fails the task.
* the migration (up, down, up) and `alembic check`.
Build the repositories with the helpers in `shared/tests/` and `pipeline/tests/` (look for how existing git
tests create a bare "remote" and a clone). Do not shell out to a real remote.

## Acceptance

Everything above, plus: `pipeline`, `agents`, `shared`, `api` and `worker` suites green; the no-oracle tripwire
tests (`agents/tests/test_graph_oracle.py`) untouched and green; `CLAUDE.md` gets one line changed in "Phase 1
status" or the Phase 2 note saying the resolver is wired, and `.claude/rules/git-and-pr-flow.md` and `agent.md`
say "wired" instead of "nothing calls it".

## Must not touch

`agents/` (the resolver and the graph are finished; if the resolver needs a change, stop and say so under "Found
elsewhere" rather than editing: it was decided that a change inside the agent module is the user's call),
`gateway/`, `verify/`, `eval/`, `worker/`, `infra/`, `api/` routes and UI. The `shared/repolace_shared/git/`
primitives may change **only** to fix a bug you can show with a failing test; say so in "Found elsewhere"
and keep the diff minimal.
