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

Both modes share three rules, stated first because they are easy to get wrong:

* **No change, no PR.** Nothing to push, and `open_pr_on_failure` does not
  override it -- that flag means "open the PR even though the checks failed", not
  "open an empty one".
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

from repolace_shared.db.models import TaskOutcome
from verify.scoring import Score, Verdict

from repolace_agents.contracts import AgentResult


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
) -> PrDecision:
    """Decide, and say why in one human-readable sentence."""
    if not open_pr_allowed:
        return PrDecision(False, "pull requests are switched off for this run")
    if not has_change:
        return PrDecision(False, "the agent left no change to propose")

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
