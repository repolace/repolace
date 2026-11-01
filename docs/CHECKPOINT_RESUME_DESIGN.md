# Checkpoint and resume for an interrupted task (design only, no code)

Status: implemented on `feat/checkpoint-resume`, not merged. It changes a decided rule (see "The rule change"); the
rule files (`agent.md`, `task-bookkeeping.md`, `benchmark.md`, `no-oracle.md`) are updated in the same change.

**Where the build departs from the first draft of this note** (each is a decision to confirm or veto):

1. **The nonce is reused, not redrawn.** The transcript already contains the delimiters written with the run's
   nonce; a fresh one would leave the old delimiters in the history of a prompt whose rules now name another.
   The snapshot keeps the run's own nonce (no more exposed than in `llm_calls.request`, which holds every prompt),
   and `run_agent` still never takes one from a caller.
2. **A moved base refuses the resume** instead of re-cloning at the recorded sha. The patches are relative to the
   recorded base; benchmark rows can never move it, and a product task whose branch moved ends FAILED
   (`resume: the target branch moved ...`) rather than measuring a different tree.
3. **`stop_class` is set only on a row that stopped abnormally** (`paused` or `failed`). A `completed` row
   (including one that hit its step cap) has NULL: it is not a stopped task.
4. **A 5xx or timeout that outlasts the retries now fails the task** (as unavailability) where it used to end as an
   `llm_error` stop that counted as the agent's failure. A rejected key stays a harness failure and is never paused.
5. **The baseline of a resumed task is the stored row** (`task_test_runs` attempt 0); the baseline suite is still run
   once more on the fresh clone, only to build the sandbox environment `verify_attempt` requires, and its result is
   discarded.
6. **Restored commit shas are new.** A commit's name includes its time, so the rebuilt scored commit is not the sha
   `task_test_runs.commit_sha` recorded for that attempt; `patch_sha` of a resumed task is a restored sha.
7. **`WorkerTimeout` is `design`** (a slow task is the task's own doing), and an operator's stop (Ctrl-C, SIGTERM, a
   deploy cancelling the task) is `unavailable`.
8. **A pause is bounded:** 24 h or 10 resumes (`WorkerSettings.paused_max_age_seconds`, `paused_max_resumes`), then
   `failed` with `runner: paused too long or resumed too often`.

## Goal and the one limit on it

A task that stops because something it depends on was **unavailable** keeps its progress and continues from it. A task
that stops because of how the **agent or model behaved** is final, exactly as today.

The limit matters for the benchmark: resuming a run the agent itself ended (step cap, budget, bad model output) would
change what the number measures.

## 1. Which stops may resume

Classify every stop into `unavailable` or `design`, at the point the stop is written, from signals that exist today.

| Stop | Class | Source today |
|---|---|---|
| Provider refusal that outlasted the retries (429, 5xx, timeout) | unavailable | `LLMCallError.unavailable` (`kind == quota`, or `transient` past the retries) |
| Process killed (SIGKILL, OOM, host crash, reaper) | unavailable | `runner: abandoned` |
| Runner or worker stopped by an operator or deploy | unavailable | `runner: interrupted`, `interrupted: <Exc>` |
| Docker daemon or sandbox infrastructure fault | unavailable | `AttemptRecord.infrastructure_error` |
| Step cap, `BudgetExceeded` (dollars, calls, wall clock) | design | `StopReason` |
| Model-side `llm_error`, no submit, no change | design | graph stop reasons |
| `GraphRecursionError`, any harness bug | design (a bug, not an outage) | existing decision |
| `auth` failure | design (needs a human, not time) | `LLMCallError.kind` |
| `WorkerTimeout` | design | `stopclass.classify_exception` (named, not imported: it lives in the worker package) |

Rule: a row resumes only if its recorded class is `unavailable`. The class is written once, by the code that ends the
row, and never inferred later from an error string.

## 2. What a checkpoint holds

Written to Postgres, not Redis. Redis here is volatile and carries only the sweeper's dedupe key; a checkpoint has to
survive whatever the task row survives, so it lives next to the row.

- **Agent state:** `messages`, `attempt`, `steps`, `attempt_steps`, `summary`, `scored_summary`, `feedback_clean`,
  `stop_reason`. All plain data. Dependencies are already kept out of `AgentState`.
- **The tree:** the clone is deleted on exit, so a state without the tree is worthless. Store the base commit sha plus a
  binary patch of everything the agent changed (commits since base and the uncommitted diff), and the sha of the last
  scored commit.
- **Budget snapshot:** `agent_seconds_used` (active time only, downtime excluded). Dollars and calls are rebuilt from the
  `llm_calls` sum, which is already durable.
- **Provenance:** `git_sha` of the code, model key, stage-model map, limits. A resume refuses to run if any differs.

What it must **not** hold (no-oracle): the unfiltered `AttemptRecord.result`, the baseline, anything naming hidden tests.
The retry policy needs one bit, "did the scored run complete", plus the feedback the agent already saw. On resume the
last attempt is rebuilt from `task_test_runs` through the same code path that builds it today (open question 3).

Cadence: after every model step and after every scored attempt, as an upsert of the latest checkpoint (keep one, plus
the previous for a torn write). A crash mid-step loses at most that step.

## 3. How a resume runs

1. A row in status `paused` (see 4) with class `unavailable` is claimed `paused -> running`, conditional, so two
   resumers cannot both win.
2. The pipeline re-runs the stages before the agent against the **recorded base sha**, not the branch head: clone, index
   (reused if fresh), baseline. The baseline is deterministic, so re-running it is cheaper than persisting it.
3. The agent stage applies the patch, loads the state, and enters the graph at the `agent` node.
4. A **fresh nonce** is drawn, as `run_agent` already requires. A nonce is never reused.
5. Budget is rebuilt from `llm_calls` and `agent_seconds_used`; the clock restarts from the stored active seconds.
6. A `resume_count` on the row is incremented. After N resumes (suggest 3) or a pause older than a set age (suggest 24 h)
   the row becomes `failed` with `runner: abandoned`, as today.

## 4. The rule change, and only this change

Today: "a task row is never re-run" (`runner.py:15`, `worker/tasks.py:8`, `benchmark.md`, `task-bookkeeping.md`).

Proposed: **a row is never re-run from scratch. A row that ended for unavailability and has a checkpoint is `paused`
and continues from the checkpoint. A row that ended by design is terminal.**

Concretely:

- New non-terminal status `paused`, with `paused_reason` and `resume_count`. The code that would write `failed` for an
  `unavailable` class writes `paused` when a checkpoint exists, and `failed` when none does.
- The worker reaper: a stale-heartbeat `running` row with a checkpoint becomes `paused`, not `failed`.
- The eval runner's two-failure breaker counts only `design` outcomes and unavailable-without-checkpoint rows, so an
  outage no longer stops the sweep; paused rows wait.
- The old workaround goes away: no archiving and deleting `llm_calls`, no resetting a failed row to `queued`, no new run
  id for rows lost to 429. `requeue_e1.sh` becomes "resume paused rows when Pro answers".

## 5. Benchmark integrity

- Report a `resumed` flag per row and the pass rate with and without resumed rows. They are never hidden.
- Cost is the sum of all `llm_calls` for the row, across processes, so nothing is lost or double counted.
- Wall seconds are active seconds. Report the pause total separately.
- A resumed row on different code or a different model is refused, so a sweep cannot mix two versions in one row.
- Rows lost before this exists keep the existing "new run id via `--rows`" path. This design does not change them.

## 6. Open questions: what reading the code found

1. **Transcript size.** Unmeasured. A snapshot is written before every agent step and replaces the previous one
   (one row per task), so the table holds one transcript per task. The transcript is bounded by the graph itself
   (`MAX_TRANSCRIPT_CHARS`, 400,000 characters, elided past that), so a row is at most that plus the patches, which
   are zlib-compressed and capped at 256 MB on read. **`llm_calls.request`** does hold the full request (the gateway
   records the messages it sent), so a transcript *could* be rebuilt from it; the snapshot is kept anyway because it
   is one read instead of an ordered scan, and because it is written at step boundaries the call log does not mark.
2. **Provider-specific message parts.** The Gemini thought signature is packed into the tool-call **id** string
   (`call_<n>~<signature>`), so it is plain text inside the messages and survives a JSON round trip by construction.
   Tested with the JSON round trip, not against the live API (the live test needs a key and a running provider).
3. **Rebuilding `last_attempt` without oracle data: resolved, yes.** `verify_attempt` returns an `AttemptRecord`
   whose `result` is exactly what `to_row` writes to `task_test_runs`, field for field, and `to_suite_result` is its
   inverse. The one field the table does not hold is `infrastructure_error`, which the snapshot keeps as a boolean in
   `ScoredMark`. The feedback the model saw is already in the transcript, so nothing derived from the oracle is
   re-derived on a resume.
4. **`TaskBudget` reload: resolved.** The budget is a plain object (`spent_usd`, `calls`, `started_at`), built per
   task and never persisted. A resume sets `calls` and `spent_usd` from `llm_calls` (a failed call is a row and
   counts as a call, as `charge(None)` counts it) and sets `started_at = clock() - agent_seconds_used`, so the wall
   cap measures the seconds the agent ran, not the time the task spent paused.
5. **Base sha for product tasks: decided against pinning.** See decision 2 at the top. A resume refuses a clone whose
   base is not the recorded one.
6. **Tokens.** Unchanged: the push refreshes its installation token right before pushing, as the git rules require.
7. **Index freshness.** A resume re-enters the same `_index` stage and lock as any task; a stale index is rebuilt.
8. **Not covered:** an `infrastructure_error` on an attempt's own suite run (`SandboxUnavailable` inside
   `verify_attempt`) is turned into an unscoreable result by `run_scored_suite`, as before, and ends the loop; it does
   not raise, so it is not classified or resumed. Making it resumable would need the scorer to refuse to record the
   run, which is a separate change.
9. **The checkpoint row is not deleted** when a task finishes. It cascades with the task. A finished row keeps one
   compressed transcript and its patches; deleting it on completion is a one-line follow-up if the size matters.

## 7. Tests the implementation must have

- Kill the process at every graph node and between stages; the resumed run's transcript prefix equals the original's.
- A fake model whose requests are checked for validity (the existing `ScriptedLLM` pattern) across a resume.
- Differential: uninterrupted run and a killed-and-resumed run reach the same scored outcome on a scripted model.
- Every `design` stop is refused a resume; every `unavailable` stop with a checkpoint is accepted; without a
  checkpoint it fails as today.
- Two concurrent resumers: exactly one claims.
- The no-oracle tripwires (`agents/tests/test_graph_oracle.py`) stay green, and the checkpoint serializer has its own
  test that no oracle-bearing field is written.
- Resume refused on a different `git_sha`, model or limits.

## 8. Where each piece lives

| Piece | Place |
|---|---|
| Storage, status, columns | migration `0017`, `shared/repolace_shared/db/models.py` |
| `unavailable` vs `design` | `LLMCallError.unavailable` (gateway), `pipeline/repolace_pipeline/stopclass.py` |
| Snapshot and its JSON | `agents/repolace_agents/checkpoint.py`, `AgentSnapshot` in `contracts.py` |
| Graph hooks and the resume entry | `agents/repolace_agents/graph.py` (`_checkpoint`, `entry`, `_resumed_state`) |
| Tree capture and restore | `TaskWorkspace.capture` / `restore`, `GitRepo.binary_diff` / `apply_patch` |
| Save and load | `pipeline/repolace_pipeline/checkpoints.py` |
| Claim, pause, resume | `pipeline/repolace_pipeline/run.py` (`_claim`, `_fail`, `_try_pause`, `_load_resume`, `_restore`, `_rebuild_budget`) |
| Reaper, sweeper, expiry | `shared/repolace_shared/tasks_recovery.py`, `worker/repolace_worker/maintenance.py` |
| Runner and breaker | `eval/harness/runner.py`, `eval/harness/db.py` |

Not built: report columns for `resume_count` (the data is on the row) and replacing the shell requeue watchers with a
resume watcher (the runner now dispatches `paused` rows itself).
