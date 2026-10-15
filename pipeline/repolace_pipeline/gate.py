"""Whether to open a pull request. Pure: no I/O, no clock, nothing to mock.

Two modes with deliberately different rules, because they answer different
questions.

**Benchmark mode** (the task has an `instance_id`) has ground truth: a curated
fail-to-pass list. A PR opens only when `score()` says PASSED, so the number of
PRs in a bench repository *is* the pass count and nothing needs recomputing.

**Product mode** has no ground truth, which is the normal case for a live issue:
the suite passes and the bug is simply not covered by it. `score()` is
inadmissible there, so it cannot gate -- a real task would never open a PR. The
gate asks the question that can be answered instead: did the agent say it was
done (`submit`), and did `agent_verdict` find no evidence of harm? That is a
weaker claim than PASSED and the PR body words it as one.

Both modes share four rules, stated first because they are easy to get wrong:

* **No change, no PR.** Nothing to push, and `open_pr_on_failure` does not
  override it -- that flag means "open the PR even though the checks failed", not
  "open an empty one".
* **A change past the scope cap is not proposed.** More than `max_changed_files` files or
  `max_changed_lines` added plus removed lines is far more than the issues this agent is
  pointed at call for, and nothing else bounds a patch. `open_pr_on_failure` does not override
  it either: that flag is about failed *checks*, and a runaway edit is not one. The reason
  carries counts only.
* **`open_pr_allowed=False` is absolute.** `--no-pr` and the gold runner use it;
  a gold validation run that opened real PRs would be a bench-repo write the
  operator did not ask for.
* **In benchmark mode the reasons never name a test.** They end up in logs and
  in `RunResult.pr_gate_reason`. The agent reads neither, so a test id there is
  not an oracle by itself; the rule keeps the curated and hidden ids out of
  operator-side text that is easy to paste or publish beside a number, the same
  rule `run._score_log_fields` applies to the score's log lines. Product mode has
  no hidden tests, so the verdict's own sentence (which names the regressions) is
  quoted: it is exactly what the person reading the result needs.
"""

from dataclasses import dataclass

from repolace_agents.contracts import AgentResult
from repolace_shared.db.models import TaskOutcome
from verify.scoring import Score, Verdict


def count_changed_lines(diff: str) -> int:
    """Added plus removed lines in a unified diff.

    Only lines inside a hunk count: a `--- a/x` header and a removed line whose text starts
    with `-- ` look alike, and the hunk state is what tells them apart. A binary change has no
    hunk, so it counts for its file and nothing else.
    """
    total = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] in ("+", "-"):
            total += 1
    return total


@dataclass(frozen=True)
class PrDecision:
    #: Shadows the builtin inside the dataclass only; it is the field name the
    #: callers were specified against.
    open: bool
    reason: str


def pr_decision(
    *,
    benchmark: bool,
    has_change: bool,
    scored: Score,
    verdict: Verdict,
    result: AgentResult,
    open_pr_on_failure: bool,
    open_pr_allowed: bool,
    changed_files: int = 0,
    changed_lines: int = 0,
    max_changed_files: int | None = None,
    max_changed_lines: int | None = None,
) -> PrDecision:
    """Decide, and say why in one human-readable sentence."""
    if not open_pr_allowed:
        return PrDecision(False, "pull requests are switched off for this run")
    if not has_change:
        return PrDecision(False, "the agent left no change to propose")
    if (max_changed_files is not None and changed_files > max_changed_files) or (
        max_changed_lines is not None and changed_lines > max_changed_lines
    ):
        return PrDecision(
            False,
            f"the change exceeds the scope cap ({changed_files} files, {changed_lines} lines; "
            f"the limits are {max_changed_files} and {max_changed_lines})",
        )

    if benchmark:
        if scored.outcome is TaskOutcome.PASSED:
            return PrDecision(True, "the benchmark task scored passed")
        label = scored.outcome.value if scored.outcome is not None else "inadmissible"
        if open_pr_on_failure:
            return PrDecision(
                True, f"opened although the benchmark task scored {label}, because the task asked for a PR on failure"
            )
        return PrDecision(False, f"the benchmark task did not pass (scored {label})")

    if result.submitted and verdict.ok:
        return PrDecision(True, "the agent submitted and no evidence of harm was found")
    if open_pr_on_failure:
        return PrDecision(
            True,
            "opened because the task asked for a PR on failure "
            f"(agent {'submitted' if result.submitted else 'did not submit'}; "
            f"checks {'clean' if verdict.ok else 'flagged the change'})",
        )
    if not result.submitted:
        return PrDecision(False, f"the agent did not submit (it stopped: {result.stop_reason.value})")
    return PrDecision(False, f"the agent submitted but the checks flagged the change: {verdict.reason}")
