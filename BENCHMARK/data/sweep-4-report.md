# Benchmark report

Agent runs: sweep-4. Gold runs: gold-3, gold-4, gold-5, gold-6.

## Read this first

- 1 row(s) are still queued or running.
- 11 harness error row(s) (status failed) are counted as non-passes in the headline and excluded from the secondary passed/admissible figure. Re-run them: an instrument failure is not an agent failure, but it is not a pass either.
- 11 LLM call(s) carry no cost; the cost figures understate spend.
- 1 instance(s) in the agent runs have no validated gold run (at least 2 passing): pylint-dev__pylint-4551.

## Headline

### Run sweep-4, model vertex-gemini-3-1-pro-preview

**Pass rate: 29 of 54 instance-runs (54%) [95% interval over 18 instances: 33%-72%]** **PARTIAL (53 of 54)**

N = 18 instances from 5 Python repositories, 3 runs each, model vertex-gemini-3-1-pro-preview, pipeline commit f02ccb3c57be22c4d487776c3fa259457154b1d1.

These instances are a filtered subset of SWE-bench Verified (pure-Python pytest repositories, gold-validated, simple-fix filter) that models have probably seen, so this is not the published Verified score and does not predict performance on unseen issues. At this N, differences under about 30 percentage points between two models on the same instances are not statistically distinguishable.

Every planned (instance, run) row is counted: 11 harness-error and 0 inadmissible row(s) (listed below) are counted as non-passes, and so is every `passed_with_test_edit`.

Secondary, not the headline: passed / admissible = 29 of 42 (69%), excluding 11 harness-error and 0 inadmissible row(s) (listed).

| run_index | passed of planned | rate |
|---|---|---|
| 0 | 12 of 18 (67%) | 67% |
| 1 | 9 of 18 (50%) | 50% |
| 2 | 8 of 18 (44%) | 44% |

Run-to-run range of the 3 run rates: 44%-67%. This holds the instances fixed; it is not an interval.

How rows are counted. The headline is passed / planned: the denominator is the run's planned grid (its manifest's instances x runs), and every planned row that did not pass is a non-pass, whatever the reason: failed, `passed_with_test_edit` (never a pass, approved or not), a harness error (status `failed`), an inadmissible row (finished, no outcome) or a row that is missing or unfinished (which withholds the headline unless `--allow-partial`). A stop reason of `llm_error` is not an exclusion: such a row is counted by its outcome. The interval is a percentile bootstrap over instances, because the runs of one instance are not independent.

## Rows counted as non-passes, by reason

### Missing (0)

_none_

### Harness errors (11)

| instance | run | eval run | detail |
|---|---|---|---|
| mwaskom__seaborn-3069 | 1 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |
| mwaskom__seaborn-3187 | 1 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |
| pallets__flask-5014 | 1 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |
| pylint-dev__pylint-4551 | 1 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |
| pylint-dev__pylint-4604 | 1 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |
| pylint-dev__pylint-4970 | 1 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |
| sphinx-doc__sphinx-7590 | 2 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |
| sphinx-doc__sphinx-9230 | 2 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |
| sphinx-doc__sphinx-9320 | 2 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |
| sphinx-doc__sphinx-9367 | 2 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |
| sphinx-doc__sphinx-9602 | 0 | sweep-4 | status=failed; stop=-; outcome=-; agent: LLMCallError: provider quota exhausted: agent call to vertex_ai/gemini-3.1-pro-preview failed after 8 retries: VertexAPIError: Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP 429 |

### Inadmissible (0)

_none_

### passed_with_test_edit (0: 0 approved, 0 pending)

Never counted as passed, approved or not.

_none_

### Unfinished (1)

| instance | run | eval run | detail |
|---|---|---|---|
| sphinx-doc__sphinx-9602 | 2 | sweep-4 | status=queued |

## By model

Counts. The model is that of each task's first LLM call; a task that fell back is labelled by its first.

| group | instances | rows | passed | failed | passed_with_test_edit | inadmissible | harness error | of which llm_error (counted by outcome) | unfinished | passed of finished |
|---|---|---|---|---|---|---|---|---|---|---|
| vertex_ai/gemini-3.1-pro-preview | 18 | 53 | 29 | 13 | 0 | 0 | 11 | 0 | 0 | 29 of 53 (55%) |
| (no model call) | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 of 0 |

## By repository

Counts, not rates: a repository holds a handful of instances.

| group | instances | rows | passed | failed | passed_with_test_edit | inadmissible | harness error | of which llm_error (counted by outcome) | unfinished | passed of finished |
|---|---|---|---|---|---|---|---|---|---|---|
| mwaskom/seaborn | 2 | 6 | 0 | 4 | 0 | 0 | 2 | 0 | 0 | 0 of 6 (0%) |
| pallets/flask | 1 | 3 | 2 | 0 | 0 | 0 | 1 | 0 | 0 | 2 of 3 (67%) |
| pylint-dev/pylint | 3 | 9 | 2 | 4 | 0 | 0 | 3 | 0 | 0 | 2 of 9 (22%) |
| pytest-dev/pytest | 6 | 18 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 18 of 18 (100%) |
| sphinx-doc/sphinx | 6 | 18 | 7 | 5 | 0 | 0 | 5 | 0 | 1 | 7 of 17 (41%) |

## Cost, latency, tokens

Per model, over its finished tasks (harness errors included: the money was spent). Latency is claim to completion: it includes the clone, indexing, the baseline run, the agent, verification and the push, excludes the time a task waited in the queue, and includes contention from the sweep's parallel runs, so it is not what one task takes alone.

The per-task budget (the $2 cap) and the wall-clock limit CENSOR cost and latency: a task stopped by either shows the limit, not what it would have cost to finish, so these figures are floors for the tasks that hit one.

### vertex_ai/gemini-3.1-pro-preview

| measure | n | median | max | mean | p95 | total |
|---|---|---|---|---|---|---|
| cost (USD) | 49 | $0.6409 | $1.7985 | $0.6593 | $1.4034 | $32.3067 |
| latency (s) | 53 | 1,080.5 | 2,350.0 | 1,154.0 | 1,774.3 | 61,163.6 |

Total spend $32.3067 over 53 finished task(s) and 29 pass(es): cost per pass $1.1140 (every finished task's cost, failures included, over the passes).
Rows without cost data: 4. Rows without both timestamps: 0.

By outcome bucket:

| bucket | tasks | median cost | max cost | median latency (s) | max latency (s) |
|---|---|---|---|---|---|
| passed | 29 | $0.6107 | $1.4224 | 1,105.4 | 1,677.0 |
| failed | 13 | $0.6555 | $1.7985 | 1,743.5 | 2,350.0 |
| harness_error | 11 | $0.6252 | $1.3749 | 892.8 | 1,659.0 |

LLM calls: 1610 (11 unpriced). Input tokens: 33,070,388 (of which cache reads 22,143,968, 67.0%). Output tokens: 502,087.

| attempts | tasks |
|---|---|
| 0 | 11 |
| 1 | 42 |

| agent stop reason | tasks |
|---|---|
| (none) | 11 |
| step_cap | 21 |
| submitted | 21 |


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
