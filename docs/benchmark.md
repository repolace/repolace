# Benchmark

After reading this page you will know what the repolace benchmark measures, what number it produced and how far to trust it, and how to reproduce the run.

The headline: **29 of 54 (53.7%)**, with a 95% interval over instances of 35 to 72%. Read the caveats in [How far to trust it](#how-far-to-trust-it) before quoting it.

## What it measures

The benchmark runs the same pipeline a product task runs, on 18 real issues, and records for each task whether it passed, what it cost and how long it took. It exists so the claim "an agent takes a GitHub issue and fixes it" comes with a success rate, a cost per task and a latency, not a demo.

The 18 instances are a filtered subset of SWE-bench Verified: pure-Python pytest repositories (pytest, sphinx, pylint, seaborn, flask), gold-validated, with a simple-fix filter. Each instance is a private `bench-<instance_id>` repository in the project's GitHub organization, with GitHub Actions disabled before the first push.

A task **passes** when every curated fail-to-pass test is green at the end, with no regression in the tests that passed at the base commit, and the agent did not edit tests. A `passed_with_test_edit` is never counted as a pass. The hidden fail-to-pass tests are laid over the sandbox export only, so the agent cannot read them or run them (see [security.md](security.md)).

## The result

Single agent, Gemini 3.1 Pro on Vertex AI (`vertex-gemini-3-1-pro-preview`), 18 instances, 3 runs each (54 planned rows), run with `--no-pr`. Per-task caps: $2, 150 model calls, 40 steps per attempt, 3 scored attempts, one hour of agent time.

| Definition | Passed | Rate | 95% interval |
|---|---|---|---|
| **sweep-4, official**: passed / planned, with the 12 rows lost to provider rate limits counted as non-passes | 29 / 54 | **53.7%** | 35.2 to 72.2 (bootstrap over instances) |
| sweep-4, secondary: passed / rows that finished (not the headline) | 29 / 42 | 69.0% | 54.0 to 80.9 (row-level Wilson) |
| sweep-4b alone: the re-run of the 12 lost rows | 6 / 12 | 50.0% | 25.4 to 74.6 (Wilson) |
| Hand-stated substitution: each lost row replaced by its re-run | 35 / 54 | 64.8% | 44.4 to 83.3 (bootstrap over instances) |

**The first row is the headline.** The last row is a calculation done by hand: `repolace-eval report` never pools runs, so 35/54 is not a tool output. It is defensible, because a row lost to HTTP 429 says nothing about the agent and the re-run used the same instances, model, limits and code. It is still not the official figure and appears only labelled as a substitution.

### Cost, latency and stop reasons

These figures are computed over the **54 final rows**, meaning the sweep-4 rows with each lost row replaced by its sweep-4b re-run. They are not figures for the 29/54 row above.

| Measure | Value |
|---|---|
| Mean cost per task | $0.751 (median $0.645, largest $1.82, below the $2 cap) |
| Cost per pass | $1.16 ($40.54 over the 35 passes of the final rows); $1.26 counting $3.64 wasted on the lost rows |
| Median task time | 1,106 s (claim to terminal write, with 1 to 3 tasks sharing the machine, image build and index waits included) |
| Tasks that ended on the step cap | 28 of 54 (52%); 11 of those passed |
| Tasks that ended by submitting | 26 of 54; 24 of those passed |

By repository, over the final rows: pytest 18/18, flask 3/3, sphinx 11/18, pylint 2/9, seaborn 1/6. Removing the six pytest instances leaves 17/36 (47.2%), so repository mix moves the headline by about 17 points.

Runs of one instance are strongly correlated (ICC 0.69), so the 54 rows carry about as much evidence as 23 independent ones. That is why the interval is taken over instances, not rows.

Full tables, tests and figures: [BENCHMARK/REPORT.md](../BENCHMARK/REPORT.md). Every number in the report is in [BENCHMARK/analysis/results.json](../BENCHMARK/analysis/results.json).

## How far to trust it

- **A first measurement, not a score.** One instance moves the rate by 5.6 points. The interval is 35 to 40 points wide. Comparing two configurations on these 18 instances needs a gap of roughly 40 points to call them different.
- **Contamination.** The instances are public SWE-bench Verified issues that models have probably seen, so a pass cannot be told apart from recall. This is not the published Verified score and does not predict performance on unseen issues.
- **Subset.** The 18 are not the official set. Six are pytest and all pass.
- **Provider loss.** Twelve rows ended as harness errors when Vertex AI answered `429 RESOURCE_EXHAUSTED` after the gateway's bounded retries. The headline counts them as non-passes.
- **Pull-request opening was not measured.** Every sweep used `--no-pr`. A run that opens real PRs has not been done.
- **The feedback filter is not an absolute barrier.** Hidden tests are kept from the agent's feedback by a filter, and a deliberately adversarial agent could still try to signal through it.
- **Variants are not measured.** The early-edit nudge, the role split and mixed models have their own run ids; none is merged, and none has been shown to beat the single agent.

## How a run is built

The `repolace-eval` subcommands, in the order a run uses them:

| Subcommand | Does |
|---|---|
| `select` | Chooses instances from SWE-bench Verified (needs network) and writes `eval/instances/<id>.json` |
| `fork` | Creates the private `bench-<id>` repositories and pushes each base commit. Needs a separate GitHub token |
| `enqueue` | Writes the run manifest, then inserts the task rows idempotently |
| `run` | Starts task subprocesses K at a time and waits for them |
| `gold` | Reads two gold runs and writes `eval/instances/VALIDATION.md` |
| `report` | Writes the report from the database and the manifest |
| `retrieval` | Evaluates retrieval strategies with no model calls |

Flags (from each module's argument parser; run `uv run --all-packages repolace-eval <subcommand> --help` for the text):

- `select`: `--count` (default 25), `--max-per-repo` (6), `--seed` (0), `--instances-dir`, `--cache-dir`, `--specs`, `--overwrite`, and dataset options `--dataset`, `--config`, `--split`, `--endpoint`.
- `fork`: `--instances` (comma-separated ids or `all`), `--instances-dir`, `--bench-repos-file`, `--cache-dir`, `--add-to-installation <id>`, `--delete` with `--yes`.
- `enqueue`: `--eval-run-id` (required), `--instances`, `--runs` (3), `--rows 'instance#index,...'` (plan only those rows, for a re-run of rows lost outside the agent), `--agent llm|stub|gold`, `--model`, `--timeout-seconds`, `--open-pr-on-failure`, `--allow-dirty-tree`, `--instances-dir`, `--bench-repos-file`, `--runs-dir`.
- `run`: `--eval-run-id` (required), `-k`/`--concurrency` (3), `--timeout-seconds`, `--agent llm|gold`, `--no-pr`, `--model`, `--max-total-usd`, `--limit`, `--mark-abandoned`, `--runs-dir`.
- `gold`: `--runs` (default `gold-1,gold-2`), `--instances-dir`, `--output`, `--runner-timeout-seconds`.
- `report`: `--run` and `--gold-run` (each repeatable), `--instances-dir`, `--runs-dir`, `--expect`/`--no-expect`, `--allow-partial`, `--supersede none|latest`, `--format markdown|json`, `--output`, `--predictions`, `--predictions-run-index`.
- `retrieval`: `--plan`, `--repos`, `--strategies`, `--query-strategies`, `--max-instances-per-repo`, `--max-chunks-per-repo` (40,000), `--max-index-equivalents` (35), `--min-gain-pp` (10.0), `--instances-dir`, `--cache-dir`, `--format`, `--output`.

### How the number is computed

`passed / planned`, where *planned* is the grid in the run's manifest (instances times runs). Every planned row that is not a pass is a non-pass, whatever the reason: a failure, a harness error, an unscoreable instance, a pass that edited tests. Excluding rows after seeing results is cherry-picking, so the headline does not. The rate over only the scoreable rows is printed as a labelled secondary figure. A planned row that is missing or still running withholds the headline unless you pass `--allow-partial`.

### Gold validation comes first

Before any agent run, the reference fix of each instance goes through the real pipeline twice at no model cost. An instance that does not score `passed` both times is dropped, not fixed. This found the instrument faults (missing dependencies, `setuptools_scm` needing `.git`, truncated test ids) that would otherwise have counted against the agent.

## Reproduce the run

The order is also in [PHASE1_TESTING.md](PHASE1_TESTING.md), with a read-only preflight script. You need the rootless Docker daemon, Postgres, a GitHub App installed on the bench repositories, and a provider key. The recorded run cost about $44 in model calls.

```bash
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock
M=vertex-gemini-3-1-pro-preview

# 1. Select instances and create the private bench repositories.
uv run --all-packages repolace-eval select
REPOLACE_BENCH_GITHUB_TOKEN=... uv run --all-packages repolace-eval fork

# 2. Gold validation: twice, no model cost, no PRs.
uv run --all-packages repolace-eval enqueue --eval-run-id gold-1 --agent gold --runs 1
uv run --all-packages repolace-eval run --eval-run-id gold-1 --agent gold
uv run --all-packages repolace-eval enqueue --eval-run-id gold-2 --agent gold --runs 1
uv run --all-packages repolace-eval run --eval-run-id gold-2 --agent gold
uv run --all-packages repolace-eval gold --runs gold-1,gold-2

# 3. A dry run on a few instances to see real cost, then the sweep.
uv run --all-packages repolace-eval enqueue --eval-run-id dry-1 --agent llm --model $M \
  --instances pallets__flask-5014,pytest-dev__pytest-7236,pylint-dev__pylint-4604 --runs 1
uv run --all-packages repolace-eval run --eval-run-id dry-1 --agent llm --model $M \
  -k 1 --max-total-usd 3 --no-pr
uv run --all-packages repolace-eval enqueue --eval-run-id sweep-1 --agent llm --model $M
uv run --all-packages repolace-eval run --eval-run-id sweep-1 --agent llm --model $M \
  -k 3 --max-total-usd <cap> --no-pr

# 4. The report.
uv run --all-packages repolace-eval report --run sweep-1 --gold-run gold-1 --gold-run gold-2
```

The bench token goes inline on the one `fork` command and is never put in `.env`. `-k` is a RAM decision: about 4 GB per child. `enqueue` refuses a modified tracked tree and records the commit, so run it from a clean checkout. A task row is never re-queued; rows lost to the provider are re-run as a new run id with `enqueue --rows`.

To regenerate the recorded analysis from the committed data:

```bash
python3 -I BENCHMARK/analysis/analyze.py
```

## The retrieval grid

`repolace-eval retrieval` measures whether retrieval finds the code a fix changed, per embedding strategy, with no model calls. The embedder reads only the first 128 tokens of a chunk, so the grid compares four index strategies (`truncate`, `head_tail`, `windows`, `signature_docstring`) by two query strategies (`truncate`, `head_tail`): eight cells, with `truncate / truncate` as the baseline.

The switch rule was written before any number existed: a cell replaces the baseline only if its paired chunk-MRR gain is at least 10 percentage points **and** its 95% bootstrap interval over instances excludes zero.

**No cell met the rule.** Over the 18 instances, the baseline chunk MRR was 7.3%. The best gains were +5.3 points for `signature_docstring / truncate` (interval 0.0 to +14.8) and +5.3 points for `windows / truncate` (interval -10.1 to +20.6). Both fall short of 10 points, and the second interval includes zero. The default strategy therefore stays `truncate`.

On repolace's own 238 chunks, 99 (41.6%) exceed the 128-token window (measured once; re-measure before quoting it for another corpus). Source: `BENCHMARK/data/retrieval.json`.

## The live reports

The public site serves two interactive reports built from this data:

- `/benchmark/`: the sweep report with figures
- `/retrieval-benchmark/`: the retrieval grid

They are static pages generated from `report.json` and `retrieval.json`; nothing on them is typed by hand except wording.

## Related

- [BENCHMARK/README.md](../BENCHMARK/README.md): file index of the recorded run
- [.claude/rules/benchmark.md](../.claude/rules/benchmark.md): design decisions of the harness
- [.claude/rules/no-oracle.md](../.claude/rules/no-oracle.md): how hidden tests stay hidden
- [Architecture](architecture.md)
