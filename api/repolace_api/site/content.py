"""What the public pages say about the benchmark, read from the packaged snapshot.

`data/results.json` is a copy of `BENCHMARK/analysis/results.json` that feeds the landing and pricing
numbers; `scripts/snapshot_benchmark.py` refreshes it and a test fails if it drifts from the source while
that exists. The full report at /benchmark/ is the static site in `benchmark_site/`. The public process reads only this package, never `eval/`
or the database, so it cannot leak a live sweep.

The headline is the official sweep-4 figure with its interval. The 35/54 "combined" figure is a
hand-stated substitution of 12 provider-lost rows by their re-run (`repolace-eval report` refuses to
pool runs), so it appears only where labelled as that, never as a headline.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "results.json"


@dataclass(frozen=True)
class BenchmarkFacts:
    passed: int
    planned: int
    rate: float
    ci_low: float
    ci_high: float
    combined_passed: int
    combined_rate: float
    combined_ci_low: float
    combined_ci_high: float
    cost_mean: float
    cost_median: float
    cost_per_pass_low: float
    cost_per_pass_high: float
    latency_median_min: int
    step_cap_share: float
    step_cap_pass_rate: float
    submitted_pass_rate: float
    lost_rows: int
    cap_usd: float


def facts_from(results: dict) -> BenchmarkFacts:
    official = results["headlines"]["sweep4_official"]
    combined = results["headlines"]["combined_final_outcomes"]
    cost = results["cost"]
    stop = results["stop_reason"]
    per_pass = sorted([cost["usd_per_pass_final_rows"], cost["usd_per_pass_all_spend"]])
    return BenchmarkFacts(
        passed=official["passed"],
        planned=official["planned"],
        rate=official["rate"],
        ci_low=official["boot_ci_instances"][0],
        ci_high=official["boot_ci_instances"][1],
        combined_passed=combined["passed"],
        combined_rate=combined["rate"],
        combined_ci_low=combined["boot_ci_instances"][0],
        combined_ci_high=combined["boot_ci_instances"][1],
        cost_mean=cost["final_rows"]["mean"],
        cost_median=cost["final_rows"]["median"],
        cost_per_pass_low=per_pass[0],
        cost_per_pass_high=per_pass[1],
        latency_median_min=round(results["latency_s"]["final_rows"]["median"] / 60),
        step_cap_share=stop["step_cap_share"],
        step_cap_pass_rate=stop["step_cap_pass_rate"],
        submitted_pass_rate=stop["submitted_pass_rate"],
        lost_rows=results["lost_rows"]["count"],
        cap_usd=cost["per_task_cap_usd"],
    )


@lru_cache
def load_results() -> dict:
    return json.loads(DATA_FILE.read_text())


def load_facts() -> BenchmarkFacts:
    return facts_from(load_results())


@dataclass(frozen=True)
class Plan:
    name: str
    price: str
    period: str
    tasks: str
    blurb: str
    features: tuple[str, ...]
    featured: bool = False


#: Preview pricing, not final and not billable yet (no payment provider is chosen). Reasoning and
#: the validation gates are in the plan; the page says "preview" in words, not in a footnote.
PLANS: tuple[Plan, ...] = (
    Plan("Free", "$0", "", "2 tasks a month", "Try it on one public repository.",
         ("1 public repository", "Managed model", "Community support")),
    Plan("Hobby", "$29", "a month", "15 tasks a month", "For one maintainer with a steady issue backlog.",
         ("Up to 3 repositories", "Managed model", "Email support")),
    Plan("Pro", "$99", "a month", "60 tasks a month", "For a small team that wants issues closed while it sleeps.",
         ("Up to 5 repositories", "Managed model", "Priority queue", "Email support"), featured=True),
    Plan("Team", "$299", "a month", "200 tasks a month", "For teams with private monorepos and their own model contracts.",
         ("Unlimited repositories", "Bring your own model key", "Priority queue", "Direct support")),
)
OVERAGE_USD = "2.25"
