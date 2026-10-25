# Benchmark report

Agent runs: sweep-4b. Gold runs: gold-3, gold-4, gold-5, gold-6.

## Read this first

- sweep-4b: instances have different numbers of runs; the headline is the pooled count, not an average of instance rates.
- 1 instance(s) in the agent runs have no validated gold run (at least 2 passing): pylint-dev__pylint-4551.

## Headline

### Run sweep-4b, model vertex-gemini-3-1-pro-preview

**Pass rate: 6 of 12 instance-runs (50%) [95% interval over 11 instances: 25%-75%]** **UNEQUAL RUNS PER INSTANCE**

N = 11 instances from 4 Python repositories, 3 runs each, model vertex-gemini-3-1-pro-preview, pipeline commit f02ccb3c57be22c4d487776c3fa259457154b1d1.

These instances are a filtered subset of SWE-bench Verified (pure-Python pytest repositories, gold-validated, simple-fix filter) that models have probably seen, so this is not the published Verified score and does not predict performance on unseen issues. At this N, differences under about 30 percentage points between two models on the same instances are not statistically distinguishable.

Every planned (instance, run) row is counted: 0 harness-error and 0 inadmissible row(s) (listed below) are counted as non-passes, and so is every `passed_with_test_edit`.

Secondary, not the headline: passed / admissible = 6 of 12 (50%), excluding 0 harness-error and 0 inadmissible row(s) (listed).

| run_index | passed of planned | rate |
|---|---|---|
| 0 | 0 of 1 (0%) | 0% |
| 1 | 2 of 6 (33%) | 33% |
| 2 | 4 of 5 (80%) | 80% |

Run-to-run range of the 3 run rates: 0%-80%. This holds the instances fixed; it is not an interval.

How rows are counted. The headline is passed / planned: the denominator is the run's planned grid (its manifest's instances x runs), and every planned row that did not pass is a non-pass, whatever the reason: failed, `passed_with_test_edit` (never a pass, approved or not), a harness error (status `failed`), an inadmissible row (finished, no outcome) or a row that is missing or unfinished (which withholds the headline unless `--allow-partial`). A stop reason of `llm_error` is not an exclusion: such a row is counted by its outcome. The interval is a percentile bootstrap over instances, because the runs of one instance are not independent.

## Rows counted as non-passes, by reason

### Missing (0)

_none_

### Harness errors (0)

_none_

### Inadmissible (0)

_none_

### passed_with_test_edit (0: 0 approved, 0 pending)

Never counted as passed, approved or not.

_none_

### Unfinished (0)

_none_

## By model

Counts. The model is that of each task's first LLM call; a task that fell back is labelled by its first.

| group | instances | rows | passed | failed | passed_with_test_edit | inadmissible | harness error | of which llm_error (counted by outcome) | unfinished | passed of finished |
|---|---|---|---|---|---|---|---|---|---|---|
| vertex_ai/gemini-3.1-pro-preview | 11 | 12 | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 6 of 12 (50%) |

## By repository

Counts, not rates: a repository holds a handful of instances.

| group | instances | rows | passed | failed | passed_with_test_edit | inadmissible | harness error | of which llm_error (counted by outcome) | unfinished | passed of finished |
|---|---|---|---|---|---|---|---|---|---|---|
| mwaskom/seaborn | 2 | 2 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 1 of 2 (50%) |
| pallets/flask | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 of 1 (100%) |
| pylint-dev/pylint | 3 | 3 | 0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 of 3 (0%) |
| sphinx-doc/sphinx | 5 | 6 | 4 | 2 | 0 | 0 | 0 | 0 | 0 | 4 of 6 (67%) |

## Cost, latency, tokens

Per model, over its finished tasks (harness errors included: the money was spent). Latency is claim to completion: it includes the clone, indexing, the baseline run, the agent, verification and the push, excludes the time a task waited in the queue, and includes contention from the sweep's parallel runs, so it is not what one task takes alone.

The per-task budget (the $2 cap) and the wall-clock limit CENSOR cost and latency: a task stopped by either shows the limit, not what it would have cost to finish, so these figures are floors for the tasks that hit one.

### vertex_ai/gemini-3.1-pro-preview

| measure | n | median | max | mean | p95 | total |
|---|---|---|---|---|---|---|
| cost (USD) | 12 | $0.9692 | $1.8215 | $0.9890 | - (n < 40) | $11.8680 |
| latency (s) | 12 | 872.3 | 2,153.0 | 1,132.6 | - (n < 40) | 13,591.0 |

Total spend $11.8680 over 12 finished task(s) and 6 pass(es): cost per pass $1.9780 (every finished task's cost, failures included, over the passes).
Rows without cost data: 0. Rows without both timestamps: 0.

By outcome bucket:

| bucket | tasks | median cost | max cost | median latency (s) | max latency (s) |
|---|---|---|---|---|---|
| passed | 6 | $0.7439 | $1.4409 | 771.9 | 1,944.7 |
| failed | 6 | $1.2360 | $1.8215 | 1,487.1 | 2,153.0 |

LLM calls: 420 (0 unpriced). Input tokens: 10,038,659 (of which cache reads 5,501,652, 54.8%). Output tokens: 141,135.

| attempts | tasks |
|---|---|
| 1 | 12 |

| agent stop reason | tasks |
|---|---|
| step_cap | 7 |
| submitted | 5 |


## Targeted pass-to-pass

0 finished row(s) over 0 instance(s) ran pass-to-pass over a targeted subset.

## Gold validation

17 of 18 gold-validated instance(s) passed 2+ times.

| instance | status | runs | detail |
|---|---|---|---|
| mwaskom__seaborn-3069 | validated | passed, passed |  |
| mwaskom__seaborn-3187 | validated | passed, passed |  |
| pallets__flask-5014 | validated | passed, passed |  |
| pylint-dev__pylint-4551 | flaky | failed, failed, passed, passed | 2 expected fail-to-pass test(s) still not passing: tests/unittest_pyreverse_writer.py::test_get_annotation_annassign[a:, tests/unittest_pyreverse_writer.py::test_get_annotation_assignattr[def |
| pylint-dev__pylint-4604 | validated | passed, passed |  |
| pylint-dev__pylint-4970 | validated | passed, passed |  |
| pytest-dev__pytest-5631 | validated | passed, passed |  |
| pytest-dev__pytest-6202 | validated | passed, passed |  |
| pytest-dev__pytest-7236 | validated | passed, passed |  |
| pytest-dev__pytest-7432 | validated | passed, passed |  |
| pytest-dev__pytest-7571 | validated | passed, passed |  |
| pytest-dev__pytest-7982 | validated | passed, passed |  |
| sphinx-doc__sphinx-7462 | validated | passed, passed |  |
| sphinx-doc__sphinx-7590 | validated | passed, passed |  |
| sphinx-doc__sphinx-9230 | validated | passed, passed |  |
| sphinx-doc__sphinx-9320 | validated | passed, passed |  |
| sphinx-doc__sphinx-9367 | validated | passed, passed |  |
| sphinx-doc__sphinx-9602 | validated | passed, passed |  |

Agent instances without validated gold: pylint-dev__pylint-4551.

## Caveats

- SWE-bench Verified is public and widely used, so these instances and their upstream fixes are likely present in the training data of the models evaluated. This figure measures the pipeline on a possibly memorised set: it shows the loop works end to end, not how it would do on unseen issues, and it is not comparable with numbers reported on other benchmarks.
- These instances are a filtered subset of SWE-bench Verified, not the official set, so the number is not the published Verified score. How the subset was chosen matters: (1) only seven repositories are allowed (pure-Python pytest projects), so most of Verified is excluded; (2) a simple-fix filter rejects instances whose gold patch touches test or configuration paths, deletes or renames a file, or is binary, so the set skews toward pure source edits; (3) instances were taken round-robin across repositories under a per-repository cap, not in proportion to Verified; (4) gold validation (which drops unscoreable and flaky instances, and records suite wall time) pruned the set further; (5) the selection seed is a degree of freedom, so it must be fixed and recorded before the first agent run. The predictions export allows a cross-check with the official SWE-bench harness, but that check uses different pass criteria (node-id format, pass-to-pass scope), so exact agreement is not expected.
- At N = 15 to 20 instances the 95% interval is about 30 to 40 percentage points wide (a Wilson interval at p = 0.5 is 30-75% for N = 15 and 30-70% for N = 20), and one instance moves the rate by 1/N. The run-to-run range is the spread of the run rates with the instances held fixed. It is not an interval and does not capture the uncertainty from having sampled N instances. At this N, differences under about 30 percentage points between two models on the same instances are not statistically distinguishable.
