"""Tests for BENCHMARK/analysis/replication_stats.py over the committed sweep-4/4b/6/6b exports.

Every number REPORT.md section 11 and docs/benchmark.md quote about sweep-6 is held here, so a re-export that changes
one must change the prose.

    uv run --all-packages pytest BENCHMARK/analysis/test_replication_stats.py -q
"""

import json
import random
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import replication_stats as R  # noqa: E402 -- the module under test; the analysis directory is not a package

pytestmark = pytest.mark.skipif(not (R.S.DATA / "sweep-6" / "tasks.csv").exists(), reason="sweep-6 is not exported")
REPO = HERE.parent.parent


@pytest.fixture(scope="module")
def report():
    return R.build()


def by_label(report):
    return {f["label"]: f for f in report["figures"]}


def pct(x):
    return round(100 * x, 1)


def test_the_figures(report):
    f = by_label(report)
    got = {k: (v["passed"], v["planned"], [pct(x) for x in v["ci_bootstrap_instances"]]) for k, v in f.items()}
    assert got == {
        "sweep-6 official": (28, 54, [31.5, 72.2]),
        "sweep-6 substituted": (31, 54, [35.2, 79.6]),
        "sweep-4 substituted (baseline)": (35, 54, [44.4, 83.3]),
        "pooled 4+6 substituted": (66, 108, [40.7, 80.6]),
    }
    assert all(x["instances"] == 18 for x in f.values())


def test_the_baseline_it_rebuilds_is_the_published_one_to_the_bit(report):
    """Same data, same bootstrap, same random stream as stats.py's "baseline", so the same floats as report.json."""
    published = next(r for r in json.loads((R.S.SITE_DATA / "report.json").read_text())["runs"] if r["id"] == "baseline")
    assert by_label(report)["sweep-4 substituted (baseline)"]["ci_bootstrap_instances"] == published["headline"]["ci_bootstrap_instances"]


def test_the_pool_is_no_narrower_than_the_baseline(report):
    f = by_label(report)
    width = {k: v["ci_bootstrap_instances"][1] - v["ci_bootstrap_instances"][0] for k, v in f.items()}
    assert pct(width["pooled 4+6 substituted"]) == 39.8 and pct(width["sweep-4 substituted (baseline)"]) == 38.9


def test_only_the_lost_rows_are_substituted(report):
    cells = report["runs"]["substituted_cells"]
    assert len(cells["sweep-4"]) == 12
    assert sorted(cells["sweep-6"]) == ["pytest-dev__pytest-7432#1", "pytest-dev__pytest-7571#1", "pytest-dev__pytest-7982#1"]


def test_the_paired_comparison_on_both_bases(report):
    sub, off = report["paired"]["substituted"], report["paired"]["official"]
    assert sub["exploratory"] is off["exploratory"] is True
    assert (sub["same"], sub["favour_sweep_4"], sub["favour_sweep_6"], sub["net_passes_sweep_6_minus_sweep_4"]) == (15, 3, 0, -4)
    assert (sub["pass_every_run_in_both"], sub["pass_no_run_in_both"]) == (10, 4)
    # Three discordant instances all one way is the most extreme outcome there is: p cannot go lower.
    assert sub["sign_flip_exact_p_two_sided"] == sub["smallest_p_possible_with_these_discordant_instances"] == 0.25
    assert (off["same"], off["favour_sweep_4"], off["favour_sweep_6"], off["net_passes_sweep_6_minus_sweep_4"]) == (10, 4, 4, -1)
    assert (off["pass_every_run_in_both"], off["pass_no_run_in_both"]) == (3, 6)
    for p in (sub, off):
        m = p["mcnemar_run_index_0"]
        assert (m["sweep_4_only"], m["sweep_6_only"], m["p_exact_two_sided"]) == (2, 0, 0.5)


def test_exact_sign_flip_against_the_monte_carlo_one():
    diffs = [0, -1, 2, 0, -2, 1, 1, -1]
    mc = R.S.sign_flip_perm(diffs, random.Random(1))
    assert abs(R.exact_sign_flip(diffs) - mc) < 0.02
    assert R.exact_sign_flip([0, 0]) == 1.0 and R.exact_sign_flip([-1, -2, -1]) == 0.25


def test_cost_uses_the_published_definitions(report):
    """The same function over sweep-4 gives the figures docs/benchmark.md publishes for it."""
    b, s = report["cost"]["sweep-4 substituted (baseline)"], report["cost"]["sweep-6 substituted"]
    assert (round(b["mean_cost_usd"], 3), round(b["median_cost_usd"], 3), round(b["cost_per_pass_usd"], 2), round(b["median_latency_s"])) == (
        0.751, 0.645, 1.16, 1106)
    assert (round(s["mean_cost_usd"], 2), round(s["median_cost_usd"], 2), round(s["cost_per_pass_usd"], 2), round(s["median_latency_s"])) == (
        0.96, 0.78, 1.67, 986)
    assert "budget_wall" not in s["stop_reasons"]  # the longer retry window never ran an agent out of its hour
    assert round(report["cost"]["spend_usd"]["total"], 2) == 52.38


def test_the_confound_travels_with_the_data(report):
    assert report["runs"]["retry_window_max_attempts"] == {"sweep-4": 9, "sweep-6": 21}
    assert "9-try" in by_label(report)["pooled 4+6 substituted"]["note"]
    assert all("9-try" in p["note"] for p in report["paired"].values())
    assert "Hand pool" in by_label(report)["pooled 4+6 substituted"]["note"]
    assert "Hand-stated substitution" in by_label(report)["sweep-6 substituted"]["note"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_retry_windows_are_what_the_manifest_shas_ran(report):
    for run, sha in report["runs"]["git_sha"].items():
        shown = subprocess.run(["git", "-C", str(REPO), "show", f"{sha}:gateway/models.toml"], capture_output=True, text=True, check=False)
        if shown.returncode != 0:
            pytest.skip(f"{sha} is not in this clone")
        assert tomllib.loads(shown.stdout)["gateway"]["max_attempts"] == R.RETRY_WINDOW[run]


def test_the_committed_json_is_current(report):
    assert json.loads(R.OUT.read_text()) == json.loads(json.dumps(report))


def test_it_does_not_reach_the_site_report():
    """sweep-6 is not a run the /benchmark/ page draws."""
    assert "sweep-6" not in R.S.META and "sweep-6b" not in R.S.META
    assert "sweep-6" not in (R.S.SITE_DATA / "report.json").read_text()
