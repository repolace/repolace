# Checkpoint and resume for an interrupted task (design only, no code)

Status: proposal. Nothing here is built. It changes a decided rule (see "The rule change"), so it needs the rule files
updated in the same change that implements it.

## Goal and the one limit on it

A task that stops because something it depends on was **unavailable** keeps its progress and continues from it. A task
that stops because of how the **agent or model behaved** is final, exactly as today.

The limit matters for the benchmark: resuming a run the agent itself ended (step cap, budget, bad model output) would
change what the number measures.

## 1. Which stops may resume

Classify every stop into `unavailable` or `design`, at the point the stop is written, from signals that exist today.

| Stop | Class | Source today |
|---|---|---|
| Provider refusal that outlasted the retries (429, 5xx, timeout) | unavailable | `LLMCallError.provider_refused`, `kind == quota` |
| Process killed (SIGKILL, OOM, host crash, reaper) | unavailable | `runner: abandoned` |
| Runner or worker stopped by an operator or deploy | unavailable | `runner: interrupted`, `interrupted: <Exc>` |
| Docker daemon or sandbox infrastructure fault | unavailable | `AttemptRecord.infrastructure_error` |
| Step cap, `BudgetExceeded` (dollars, calls, wall clock) | design | `StopReason` |
| Model-side `llm_error`, no submit, no change | design | graph stop reasons |
| `GraphRecursionError`, any harness bug | design (a bug, not an outage) | existing decision |
| `auth` failure | design (needs a human, not time) | `LLMCallError.kind` |
| `WorkerTimeout` | design unless the clock was spent inside provider retry waits (decide in review) | open |

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

## 6. Risks and open questions

1. **Transcript size.** Full messages with tool output per step may be large. Compress; keep only the latest. Measure
   before choosing between a snapshot and rebuilding from `llm_calls` (unverified that `llm_calls.request` carries the
   full message list).
2. **Provider-specific message parts.** Gemini thought signatures are packed into messages; they must round trip
   through JSON unchanged. Test it against the live tool-call round trip.
3. **Rebuilding `last_attempt` without oracle data.** I have not read how `verify_attempt` derives the feedback, so
   whether `task_test_runs` alone is enough is unverified.
4. **Base sha for product tasks.** Benchmark rows pin a base commit. A product task clones the branch head, so the base
   sha must be recorded at clone time.
5. **Tokens.** A one-hour installation token may be gone on resume; refresh before the push, as the git rules already
   require.
6. **Index freshness.** Resume re-enters the index lock path; a stale index must be rebuilt, not trusted.
7. **Reclassifying `WorkerTimeout`.** Decide whether time spent in provider retry waits counts as unavailability.

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

## 8. Build order (when approved)

1. Rule files and this note reviewed, `WorkerTimeout` decided.
2. Migration: `task_checkpoints`, `paused` status, `paused_reason`, `resume_count`, stop class. `alembic check`.
3. Classification at the code that ends a row.
4. Checkpoint writer in the graph and the pipeline, with the serializer tests.
5. Resume path: claim, pinned clone, patch apply, state load, budget rebuild.
6. Reaper, runner breaker and `--mark-abandoned` changes.
7. Report columns; replace the requeue watcher with a resume watcher.
