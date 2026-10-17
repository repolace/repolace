# Phase 1: running the benchmark locally

This is the order of operations that produces the Phase 1 number. It runs **on the machine that has the
rootless Docker daemon, the development database, the 18 registered `repolace/bench-*` repositories and the
provider key**. A cloud session cannot do it (no Docker, no database, no secrets); cloud sessions work on
the code packages in `docs/cloud-work/` instead.

The headline model for this run is **Gemini 3.1 Pro on Vertex AI**, key `vertex-gemini-3-1-pro-preview`.
Nothing here has been run against a real task yet: every figure for cost, wall time and memory below is an
estimate to be replaced by what the smoke test and dry run measure.

## 0. Before anything

```bash
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock     # a real environment variable, not a .env line
cd infra && docker compose up -d postgres && cd ..           # Phase 1 needs Postgres only
scripts/phase1_preflight.sh                                  # read-only; must end "preflight passed"
```

Do **not** start the compose `api` or `worker` services for this: the worker cannot run a task as deployed
yet (package A), and the `api` container loads the whole `.env`. The benchmark runs `repolace-run-task`
subprocesses on the host.

The preflight checks git, `prlimit`, `uv`, the rootless socket and cgroup delegation, that `.env` has a
`GOOGLE_API_KEY` and no bench token, that the database is at head and the 18 bench repos match the 18
instance files, and that the model is priced and keyed for the `agent` and `resolve` stages. It makes no call.

Also confirm, once, in your Cloud console, the per-million-token prices in `gateway/models.toml` for the
two Vertex models (they were read from Google's AI Studio page, not Vertex's own).

## 1. Smoke test: one instance, one run, no PR

The cheapest real end-to-end run. Pick one instance (a Flask one is usually the quickest suite).

```bash
M=vertex-gemini-3-1-pro-preview
uv run --all-packages repolace-eval enqueue --eval-run-id smoke-1 --agent llm --model $M \
  --instances pallets__flask-5014 --runs 1
uv run --all-packages repolace-eval run --eval-run-id smoke-1 --agent llm --model $M \
  -k 1 --max-total-usd 1 --no-pr
```

`enqueue` refuses a modified tracked tree and records the commit; run it from `main`.

Read the result:

```bash
tail -n 60 eval/runs/smoke-1/*.log
docker compose -f infra/docker-compose.yml exec postgres psql -U repolace repolace -c \
 "select instance_id, status, outcome, agent_stop_reason, cost_usd, left(error_message,120) as error
    from tasks where eval_run_id='smoke-1'"
docker compose -f infra/docker-compose.yml exec postgres psql -U repolace repolace -c \
 "select c.stage, c.model, c.input_tokens, c.cached_input_tokens, c.output_tokens, c.cost_usd,
         c.response->'choices'->0->>'finish_reason' as finish, left(c.error,80) as error
    from llm_calls c join tasks t on t.id=c.task_id where t.eval_run_id='smoke-1' order by c.created_at"
```

**Success** means the task reached a terminal status with a real `agent_stop_reason` (`submitted`, `step_cap`,
`max_attempts`, ...) and an outcome; **passing is not required**. What you are checking is the machinery:

| Look at | Healthy | Not healthy, and what it means |
|---|---|---|
| `status` | `completed` | `failed`: read `error_message`. A `provider auth failure:` or `provider quota exhausted:` prefix is the provider refusing us (fix the key or quota), anything else is a repolace bug |
| `llm_calls.error` | empty on every row | an `unpriced:` error: cached or missing usage the price table cannot price; stop and fix before spending more |
| `finish` | mostly `stop` / `tool_calls` | many `length` rows: Gemini's reasoning is eating the 8192-token output cap; many `malformed_function_call`: note the count |
| `cached_input_tokens` | non-zero after the first calls | always 0: caching is not reaching the price, so cost is overstated |
| `cost_usd` | plausible against your Cloud console for the same minutes | a large mismatch: the price table is wrong |
| log | the stages run in order (clone, index, baseline, agent, verify) | the baseline `unusable` message: the instance never ran the agent |

Write down: cost of the task, wall time, number of model calls, number of attempts, suite wall time.

## 2. Dry run: three instances, one run each, no PR

Three instances from different repositories (for example one Flask, one pytest, one pylint). Cap total spend at $3.
The cap is checked before each dispatch, so with `-k 1` the total can pass it by at most one task's cost.

```bash
uv run --all-packages repolace-eval enqueue --eval-run-id dry-1 --agent llm --model $M \
  --instances pallets__flask-5014,pytest-dev__pytest-7236,pylint-dev__pylint-4604 --runs 1
uv run --all-packages repolace-eval run --eval-run-id dry-1 --agent llm --model $M \
  -k 1 --max-total-usd 3 --no-pr
uv run --all-packages repolace-eval report --run dry-1 --allow-partial
```

From this you get the numbers the sweep needs: **mean cost per task**, **mean wall time per task**, and how
often the agent stops on each reason. Sweep cost is roughly `mean cost x 18 x runs per instance`.

## 3. The sweep: only after you state a cap

18 instances x 3 runs = 54 tasks by default. Decide the cap from the dry run, then:

```bash
uv run --all-packages repolace-eval enqueue --eval-run-id sweep-1 --agent llm --model $M
uv run --all-packages repolace-eval run --eval-run-id sweep-1 --agent llm --model $M \
  -k 3 --max-total-usd <cap>
```

* `-k` is a RAM decision (about 4 GB per child); use `-k 1` if the machine is busy or Vertex throttles you.
* The runner stops dispatching after **two tasks of this invocation** fail with a provider-refusal prefix, and
  prints why. Fix the key or quota and re-run `run`; queued rows are still there. Failed rows are never re-queued.
* A rate limit that outlasts the retries counts as a provider refusal (a recoverable throttle can cost a task).
  If you see `provider quota exhausted:` with a per-minute Vertex limit, lower `-k` rather than raising anything.
* Interrupt with Ctrl-C: the runner kills its children. `run --mark-abandoned` cleans stale `running` rows.

## 4. The report

```bash
uv run --all-packages repolace-eval report --run sweep-1 \
  --gold-run gold-3 --gold-run gold-4 --output eval/runs/sweep-1/report.md
```

`pylint-dev__pylint-4551` was accepted by the later pair `gold-5`/`gold-6`, not `gold-3`/`gold-4`; if the report
flags it, that is the known reason (see `.claude/rules/benchmark.md`). The headline is `passed / planned`: every
planned row that is not a pass is a non-pass, whatever the reason. Quote the contamination, subset and small-N
caveats beside the number. At 18 instances the 95% interval is roughly 30 to 40 points wide.

## What to do when something fails

| Symptom | Do |
|---|---|
| preflight FAIL | fix that line; do not run anything else |
| exit code 2 from `repolace-run-task` | an unpriced model or a missing key; the message names which |
| `status=failed`, `provider auth failure:` | key rejected or expired: fix `.env`, re-run the live test, then `run` again |
| `status=failed`, `provider quota exhausted:` | Vertex quota or a rate limit: wait, lower `-k`, check the quota page |
| `status=failed`, anything else | a repolace bug: copy `error_message`, the task log and the `task_stages` rows |
| many `agent_stop_reason=step_cap` | report it; do not tune the agent mid-benchmark |
| `unusable baseline` in the log | that instance's suite did not run in the sandbox; report which one |
| the runner reports `runner: timeout` | the per-task wall clock fired (11,700 s default); note the instance |

## Not part of Phase 1, so do not chase it now

The compose `worker`/`api` deployment, RabbitMQ `consumer_timeout`, database indexes, the browser check of the
UI, the agent role split (`phase-2/agents`), conflict-resolver wiring, OpenTelemetry. Cloud packages A to D in
`docs/cloud-work/` own them.

## Afterwards

Put the measured numbers (the headline, mean cost and wall time per task, suite wall times, stop-reason counts)
into `CLAUDE.md` "Phase 1 status" and remove the "not yet measured" wording the run has replaced. If the run
tells you the agent overloads in a specific way, that is the input for deciding about the role split.
