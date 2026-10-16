---
description: "Task success criteria: fail-to-pass, pass-to-pass, outcomes"
paths:
  - "verify/**"
  - "pipeline/**"
  - "api/alembic/**"
  - "eval/**"
---

## Task success criteria (decided)

A task counts as successful when **the issue is resolved and the test suite passes without the agent having edited the tests** — the one exception being where editing a test is genuinely part of resolving the issue, because the test itself was wrong.

Operationally, scored as fail-to-pass plus pass-to-pass:

1. **Baseline.** Before applying any patch, run the suite in the sandbox and record which tests pass and which fail. Without this, "the tests pass" is unfalsifiable — a repo with pre-existing failures would score as a failure no matter what the agent did.
2. **Fail-to-pass.** The tests targeting the issue must go from failing to passing.
3. **Pass-to-pass.** Everything that passed at baseline must still pass. This is what catches a fix that trades one bug for another.
4. **No test edits**, by default. A diff touching test files fails the task.

**Amendment (a false PASSED, found by audit and reproduced).** "Fail-to-pass" was implemented as `baseline.failed ∪ baseline.skipped` intersected with `attempt.passed`, on the reasoning that pytest reports an xfail as a skip. It does — but so does every *ordinary* skip, and a real suite is full of them: `importorskip` for an optional dependency, a platform guard, a marker gate. A baseline skip that started passing for any reason at all — a dependency appearing in the image, an install-step change — then scored the task **PASSED with nothing red at the base commit**. That is the failure mode this whole section exists to prevent: not a point lost, but a pass nobody could defend.

The report parser now separates `xfailed` from `skipped`, using the `xfail` flag the plugin already recorded and the host discarded. The flag is **per-phase** (pytest sets `wasxfail` on the `call` report, so the surrounding phases carry False) and is **also** set on a non-strict xpass, which is a `passed` record — so the rule reads it only off the record that carried the skip. Three consequences, load-bearing *together*:

- **fail-to-pass** joins on `failed ∪ xfailed`. An ordinary skip going green is no longer evidence of anything.
- **neutralized** counts `xfailed` as silenced. Marking a baseline failure `@pytest.mark.xfail` is the cheapest possible way to make it stop objecting, and separating the buckets without this would have opened a wider hole than it closed.
- **admissibility** gates on the same `failed ∪ xfailed` expression, through one shared function so the two cannot drift. The old gate read `not failed and not skipped`, so a single ordinary skip made an unscoreable instance look scoreable; it then fell through to "no baseline-failing test now passes" and scored FAILED — an instrument limitation charged to the agent, on most real repositories.

`task_test_runs.xfailed` (migration 0010) exists for the reason 0009 gives for the sets it added: a dropped set cannot be recovered, and "was this an xfail or an ordinary skip" is exactly the question this revision had to ask.

**Amendment (the exemption was too wide).** Point 4, "no test edits", is enforced by `disqualifying_paths`, which combines what pytest actually collected with a path heuristic and then *exempts* a heuristic match that existed at the base commit — so a shipped module like `django/test/client.py` is not mistaken for a test. That exemption was exempting far too much. `tests/helpers.py`, `tests/__snapshots__/*` and `tests/cassettes/*` all existed at baseline and pytest collects tests from none of them, which is exactly the signature the exemption keyed on — so all three were freely editable, and editing a golden file or a recorded cassette is the cheapest fake fix there is. `_FIXTURE_DIR_PARTS` was protecting nothing whenever `baseline_files` was supplied.

The exemption now applies only when no *test-named ancestor directory of the file* is one pytest collected tests from. `django/test/client.py` is readmitted (nothing was collected at or under `django/test`); the three above are refused.

Two boundaries in that rule are deliberate, and loosening either breaks honest work rather than catching cheating: the directory must be **test-named**, or `myapp/models.py` beside Django's `myapp/tests.py` would be disqualified; and the repository root is excluded from the collected-directory set, because it is an ancestor of everything. Narrowing the *exemption* rather than adding a new disqualifying branch keeps both safe by construction — `is_test_path` is False for those two paths, so they never reach the exemption at all — but the tests pin them anyway, because the alternative shape is what someone simplifying this later would reach for.

The acknowledged gap: a repository whose tests sit at the top level gets no tree rule, only the heuristic and the collected-file set.

**The exception is the hard part.** "Unless the test itself was wrong" is not machine-checkable — an agent that cannot pass a test can always claim the test is at fault, and that is precisely the loophole it will find. An unfalsifiable escape hatch would quietly destroy the credibility of the benchmark number, which is the project's whole differentiation claim.

So the exception is a **separately tracked outcome, never a silent pass**:

| outcome | meaning |
|---|---|
| `passed` | fail-to-pass + pass-to-pass, source changes only |
| `passed_with_test_edit` | as above, but the diff touched tests; requires the agent to state why, and human sign-off before it counts |
| `failed` | anything else |

The headline benchmark figure is **`passed` only**. `passed_with_test_edit` is reported alongside it, never folded in. If that second bucket grows, it is a signal the agent is learning to argue with tests rather than fix code — worth watching as its own metric.

**Decided since (these were "still to pin down"):** the agent may **not** add test files. The leaning recorded here was yes, and it was reversed when the toolbox was built. The mechanical reason is that a test file the agent adds is one the scorer rejects regardless: `disqualifying_paths` applies the path heuristic to any file that did not exist at the base commit, so a task whose diff adds `tests/test_new.py` fails with "diff touches test or config files", and letting the tool create one would only waste the run. (The project plan lists "agent may not add test files" among its settled decisions; the further reasoning is not recorded in the sources this section was written from.) `create_file` therefore refuses test and configuration paths exactly as `edit_file` does (the shared `is_protected` write guard), the system prompt says so in as many words, and the tool's refusal tells the model to reproduce behaviour with `run_python` scratch scripts instead. **Decided since: the PR is capped, the run is not.** `pr_decision` withholds the PR for a change over `max_changed_files` (default 10) or `max_changed_lines` (default 500, added plus removed, counted from the reviewed diff by `count_changed_lines`), in both modes, and `open_pr_on_failure` does not override it. The defaults sit well above the 18 gold patches (at most 4 files and 126 lines). The task still finishes and is scored; the refusal is only in `pr_gate_reason`, with counts. The agent itself is still bounded only by the step, attempt and budget caps and by what the write guard refuses, so a runaway edit costs the budget, not a PR.

**Where this lives in the schema (migrations 0007 and 0011).** Until then none of the above was expressible — the project's headline claim had no table behind it.

- `tasks.outcome` is a separate enum from `tasks.status`, because they answer different questions: status is how far the pipeline got, outcome is whether the issue was fixed. Both are needed; neither substitutes for the other.
- `task_test_runs` holds one row per suite execution — attempt 0 is the baseline, 1..N are the bounded retries — storing the raw `passed`/`failed` sets rather than derived fail-to-pass and pass-to-pass lists. The scoring rule still has open questions above, so keeping what it is computed *from* means the benchmark can be rescored without re-running anything. A run that produced no usable result at all (import error, missing dependency, timeout) sets `error`, which is what makes a task *unscoreable* rather than failed.
- Migration 0011 adds `collected_files`, `conftests`, `fingerprint` and `stdout_tail`, and the first three are read by the rule rather than merely nice to have: `disqualifying_paths` joins on what pytest actually collected, and `fingerprint_changed` refuses to compare two runs whose rootdir, watched ini options or registered plugins differ. Without them a stored run cannot be rescored, only re-run — which defeats the reason 0007 stores raw sets at all.
- The test-edit exception is enforced by check constraints, not convention: `passed_with_test_edit` without a justification cannot be inserted, and sign-off cannot be recorded on any other outcome. The one part of the criteria that is not machine-checkable is at least made impossible to record silently.

**Unrelated trap found while doing this, now fixed:** migration 0004 creates the GIN index on `content_tsv` and the HNSW index on `embedding` — both arms of hybrid retrieval — but neither was declared in `CodeChunk.__table_args__`. The model and the database therefore disagreed, and `alembic revision --autogenerate` emitted a `drop_index` for each. The next autogenerated migration would have deleted the indexes retrieval depends on, with nothing failing to show for it. Both are now declared on the model, and `alembic check` is clean. **Run `alembic check` after touching models** — it is what caught this.

