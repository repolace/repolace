"""Tests for BENCHMARK/analysis/stats.py: every statistic against scipy or a closed form, the sweep-4/4b regression, PARTIAL
marking, and the rule that nothing is pooled.

    uv run --all-packages pytest BENCHMARK/analysis/test_benchmark_stats.py -q
"""

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import stats as sps

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stats as S  # noqa: E402  (the module under test; the analysis directory is not a package)

BENCH = HERE.parent


# ----------------------------------------------------------------------------------------------- closed forms
def test_wilson_matches_scipy():
    for k, n in [(0, 10), (3, 10), (35, 54), (54, 54), (1, 3)]:
        lo, hi = S.wilson(k, n)
        ref = sps.binomtest(k, n).proportion_ci(confidence_level=0.95, method="wilson")
        assert lo == pytest.approx(ref.low, abs=2e-4)
        assert hi == pytest.approx(ref.high, abs=2e-4)


def test_quantile_matches_numpy_linear():
    xs = sorted([3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5])
    for q in (0.0, 0.025, 0.25, 0.5, 0.9, 0.975, 1.0):
        assert S.quantile(xs, q) == pytest.approx(float(np.quantile(xs, q)))
    assert S.quantile([], 0.5) is None


def test_mcnemar_exact_values_and_scipy():
    # the examples in BENCHMARK/REPORT.md: 8:0 -> 0.008, 7:1 -> 0.07, 6:2 -> 0.289
    assert S.mcnemar_exact(8, 0) == pytest.approx(2 * 0.5**8)
    assert S.mcnemar_exact(7, 1) == pytest.approx(2 * 9 * 0.5**8)
    assert S.mcnemar_exact(6, 2) == pytest.approx(2 * 37 * 0.5**8)
    for b, c in [(3, 0), (4, 4), (0, 0), (10, 2)]:
        n = b + c
        ref = 1.0 if n == 0 else sps.binomtest(min(b, c), n, 0.5).pvalue
        assert S.mcnemar_exact(b, c) == pytest.approx(ref)
    assert S.mcnemar_exact(2, 5) == S.mcnemar_exact(5, 2)


def test_holm_by_hand_and_monotone():
    adj = S.holm([0.01, 0.04, 0.03])
    assert adj == pytest.approx([0.03, 0.06, 0.06])
    assert all(a >= p for a, p in zip(adj, [0.01, 0.04, 0.03], strict=True))
    assert S.holm([0.5]) == [0.5]
    assert S.holm([0.6, 0.7])[1] <= 1.0


def test_cohens_h_closed_form():
    assert S.cohens_h(0.5, 0.5) == pytest.approx(0.0)
    assert S.cohens_h(1.0, 0.0) == pytest.approx(math.pi)
    assert S.cohens_h(0.65, 0.35) == pytest.approx(2 * math.asin(math.sqrt(0.65)) - 2 * math.asin(math.sqrt(0.35)))


def test_pass_at_k_closed_form():
    assert S.pass_at_k(0, 3, 3) == 0.0
    assert S.pass_at_k(1, 3, 1) == pytest.approx(1 / 3)
    assert S.pass_at_k(1, 3, 3) == 1.0
    assert S.pass_at_k(2, 3, 2) == pytest.approx(1 - math.comb(1, 2) / math.comb(3, 2))
    assert S.pass_at_k(1, 4, 2) == pytest.approx(1 - math.comb(3, 2) / math.comb(4, 2))


def test_icc_matches_anova_identity_and_extremes():
    groups = [[1, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 0, 0]]
    icc, _msb, _msw = S.icc_oneway(groups)
    f = sps.f_oneway(*groups).statistic
    m = 3
    assert icc == pytest.approx((f - 1) / (f + m - 1))
    assert S.icc_oneway([[1, 1, 1], [0, 0, 0], [1, 1, 1]])[0] == pytest.approx(1.0)
    assert S.icc_oneway([[1, 0, 1], [1, 0, 1], [1, 0, 1]])[0] == pytest.approx(-0.5)


def test_cochran_q_equals_mcnemar_chi_square_for_two_columns():
    # for k = 2 columns, Cochran's Q is (b - c)^2 / (b + c), the McNemar chi-square
    matrix = [[1, 0]] * 5 + [[0, 1]] * 2 + [[1, 1]] * 4 + [[0, 0]] * 3
    q = S.cochran_q(matrix)
    assert q["q"] == pytest.approx((5 - 2) ** 2 / (5 + 2))
    assert q["df"] == 1
    assert q["p"] == pytest.approx(float(sps.chi2.sf(q["q"], 1)))
    assert S.cochran_q([[1, 1, 1], [0, 0, 0]])["q"] == 0.0  # rows differ, columns do not: no between-column effect
    assert S.cochran_q([[1, 0, 0]] * 3)["q"] == pytest.approx(6.0)  # column 0 passes everything, columns 1 and 2 nothing
    assert S.cochran_q([[1, 1], [0, 0]]) == {"q": 0.0, "df": 1, "p": 1.0}


def test_auc_less():
    assert S.auc_less([1, 2], [5, 6]) == 1.0
    assert S.auc_less([5, 6], [1, 2]) == 0.0
    assert S.auc_less([3], [3]) == 0.5
    assert S.auc_less([], [1]) is None


def test_mantel_haenszel_by_hand():
    # two strata (a, b, c, d): variant pass/fail, baseline pass/fail
    res = S.mantel_haenszel([(3, 0, 1, 2), (2, 1, 0, 3)])
    assert res["defined"] is False  # b*c is 0 in both tables: the odds ratio is infinite, so it must say undefined
    strata = [(3, 1, 1, 3), (2, 2, 1, 3), (4, 1, 2, 3)]
    res = S.mantel_haenszel(strata)
    ns = [8, 8, 10]
    num = sum(a * d / n for (a, b, c, d), n in zip(strata, ns, strict=True))
    den = sum(b * c / n for (a, b, c, d), n in zip(strata, ns, strict=True))
    assert res["defined"] is True
    assert res["odds_ratio"] == pytest.approx(num / den)
    lo, hi = res["ci"]
    assert lo < res["odds_ratio"] < hi
    assert res["informative_strata"] == 3


def test_mantel_haenszel_degenerate_strata_are_undefined_not_a_number():
    # the always-pass pytest instances: both groups pass every run, so no stratum carries information
    res = S.mantel_haenszel([(3, 0, 3, 0)] * 6)
    assert res["defined"] is False and res["informative_strata"] == 0
    # one informative stratum is still too few
    assert S.mantel_haenszel([(3, 0, 3, 0), (2, 1, 1, 2)])["defined"] is False


def test_bootstrap_is_deterministic_and_degenerate_cases():
    per = {f"i{k}": (3, 3) for k in range(18)}
    ci = S.boot_ci(per, S.rng_for("t"), S.rate_of)
    assert (ci["lo"], ci["hi"]) == (1.0, 1.0)
    per0 = {f"i{k}": (0, 3) for k in range(18)}
    assert S.boot_ci(per0, S.rng_for("t"), S.rate_of)["hi"] == 0.0
    mixed = {f"i{k}": (k % 4, 3) for k in range(18)}
    a = S.boot_ci(mixed, S.rng_for("x"), S.rate_of)
    b = S.boot_ci(mixed, S.rng_for("x"), S.rate_of)
    assert (a["lo"], a["hi"]) == (b["lo"], b["hi"])
    assert a["lo"] < sum(v[0] for v in mixed.values()) / 54 < a["hi"]
    assert a["n_resamples"] == S.BOOT


def test_sign_flip_perm_null_and_signal():
    assert S.sign_flip_perm([0, 0, 0], S.rng_for("p")) == 1.0
    p = S.sign_flip_perm([1] * 12, S.rng_for("p"))
    assert p == pytest.approx(2 / 4096, abs=0.002)  # only all-plus and all-minus are as extreme
    assert S.sign_flip_perm([1, -1, 1, -1], S.rng_for("p")) > 0.9


@pytest.mark.parametrize("row, expected", [
    (None, "unfinished"),
    ({"status": "queued", "outcome": None}, "unfinished"),
    ({"status": "running", "outcome": None}, "unfinished"),
    ({"status": "failed", "outcome": None}, "harness_error"),
    ({"status": "completed", "outcome": None}, "inadmissible"),
    ({"status": "completed", "outcome": "passed_with_test_edit"}, "passed_with_test_edit"),
    ({"status": "completed", "outcome": "passed"}, "passed"),
    ({"status": "completed", "outcome": "failed"}, "failed"),
])
def test_bucket_follows_benchmark_md(row, expected):
    assert S.bucket(row) == expected


def test_passed_with_test_edit_is_never_a_pass():
    assert not S.passed({"status": "completed", "outcome": "passed_with_test_edit"})
    assert not S.passed(None)
    assert S.passed({"status": "completed", "outcome": "passed"})


# ------------------------------------------------------------------------------------- sweep-4 / 4b regression
@pytest.fixture(scope="module")
def sample():
    return S.build_report(sample=True)


@pytest.fixture(scope="module")
def full():
    return S.build_report(sample=False)


def run_of(report, rid):
    return next(r for r in report["runs"] if r["id"] == rid)


def test_sweep4_official_headline(sample):
    r = run_of(sample, "sweep-4")
    assert (r["passed"], r["planned"]) == (29, 54)
    assert not r["partial"]
    assert r["buckets"]["harness_error"] == 12
    assert r["headline"]["rate"] == pytest.approx(29 / 54)


def test_baseline_substitution_reproduces_the_published_number(sample):
    b = run_of(sample, "baseline")
    assert (b["passed"], b["planned"]) == (35, 54)
    lo, hi = b["headline"]["ci_bootstrap_instances"]
    assert round(100 * lo, 1) == 44.4 and round(100 * hi, 1) == 83.3  # exactly the figures in results.json
    assert b["clustering"]["icc_oneway"] == pytest.approx(0.689, abs=0.001)
    assert b["clustering"]["design_effect_3_runs"] == pytest.approx(2.38, abs=0.01)
    assert b["clustering"]["effective_n_rows"] == pytest.approx(22.7, abs=0.1)
    assert b["behaviour"]["step_cap_share"] == pytest.approx(28 / 54)
    assert b["cost"]["per_pass"]["usd"] == pytest.approx(1.16, abs=0.01)


def test_matches_existing_results_json(sample):
    old = json.loads((HERE / "results.json").read_text())
    b = run_of(sample, "baseline")["headline"]
    h = old["headlines"]["combined_final_outcomes"]
    assert (b["passed"], b["planned"]) == (h["passed"], h["planned"])
    assert [round(100 * x, 1) for x in b["ci_bootstrap_instances"]] == h["boot_ci_instances"]
    assert round(100 * b["wilson_rows_context_only"][0], 1) == h["wilson_ci_rows_ignoring_clustering"][0]
    # sweep-4 official: a different random stream, and the percentile lands on a 1/54 step, so allow two points on the interval
    s4 = run_of(sample, "sweep-4")["headline"]
    h4 = old["headlines"]["sweep4_official"]
    lo, hi = (round(100 * x, 1) for x in s4["ci_bootstrap_instances"])
    assert abs(lo - h4["boot_ci_instances"][0]) <= 2.0 and abs(hi - h4["boot_ci_instances"][1]) <= 2.0
    adm = run_of(sample, "sweep-4")["secondary_admissible"]
    assert (adm["passed"], adm["admissible"]) == (29, 42)
    per = {i["instance_id"]: i["passes"] for i in run_of(sample, "baseline")["instances"]}
    assert sorted(i for i, n in per.items() if n == 3) == sorted(old["instances"]["always_pass"])
    assert sorted(i for i, n in per.items() if n == 0) == sorted(old["instances"]["never_pass"])
    assert sorted(i for i, n in per.items() if 0 < n < 3) == sorted(old["instances"]["mixed"])


def test_substitution_is_labelled_and_not_a_pool(sample):
    b = run_of(sample, "baseline")
    assert b["kind"] == "baseline-substitution"
    assert b["substitution"]["hand_stated"] is True
    assert len(b["substitution"]["substituted_cells"]) == 12
    assert len(b["tasks"]) == 54  # one row per cell, never 54 + 12
    ids = [r["id"] for r in sample["runs"]]
    assert ids == ["sweep-4", "sweep-4b", "baseline"]
    assert "pooled" not in json.dumps(sample["runs"][2]["headline"]).lower()
    assert run_of(sample, "sweep-4b")["planned"] == 12  # the re-run is a 12-row subset on its own


# ------------------------------------------------------------------------------------------ partial runs, comparisons
def test_partial_runs_are_marked_and_not_compared_on_a_rate(full):
    s5, r1 = run_of(full, "sweep-5"), run_of(full, "sweep-r1")
    assert s5["partial"] and s5["partial_mark"] == "PARTIAL (34 of 54)"
    assert r1["partial"] and r1["partial_mark"] == "PARTIAL (10 of 54)"
    assert "clustering" not in s5 and "clustering" not in r1  # ICC needs full instances
    variants = [c["variant"] for c in full["comparisons"]]
    assert "sweep-r1" not in variants
    c5 = next(c for c in full["comparisons"] if c["variant"] == "sweep-5")
    assert c5["partial"] is True and c5["variant_rate"] is None and c5["exploratory"] is None
    assert c5["primary"]["mcnemar_run_index_0"]["pairs"] == 18  # all 18 run_index 0 rows finished


def test_primary_comparison_is_the_preregistered_rule(full):
    fam = ["sweep-e1", "sweep-r2", "sweep-m1"]
    cs = [c for c in full["comparisons"] if c["variant"] in fam]
    assert [c["variant"] for c in cs] == fam or sorted(c["variant"] for c in cs) == sorted(fam)
    raw = [c["primary"]["mcnemar_run_index_0"]["p_exact_two_sided"] for c in cs]
    adj = [c["primary"]["mcnemar_run_index_0"]["p_holm_adjusted"] for c in cs]
    assert adj == pytest.approx(S.holm(raw))
    for c in cs:
        m = c["primary"]["mcnemar_run_index_0"]
        assert m["pairs"] == m["baseline_only_passed"] + m["variant_only_passed"] + m["both_passed"] + m["both_failed"]
        assert m["p_exact_two_sided"] == pytest.approx(S.mcnemar_exact(m["baseline_only_passed"], m["variant_only_passed"]))
        assert set(c["primary"]["secondary_metrics"]["variant"]) >= {
            "median_first_edit_call", "step_cap_share", "median_cost_per_task_usd", "submitted_share"}
        assert c["exploratory"]["label"].startswith("Exploratory")
    assert not any(c["primary"]["mcnemar_run_index_0"]["p_holm_adjusted"] < 0.05 for c in cs)  # no variant beats or loses to the baseline


def test_no_rasch_and_no_pooled_posterior(full):
    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)

    names = {k.lower() for k in keys(full)}
    assert not any("rasch" in k or "posterior" in k or "beta_binomial" in k for k in names)
    assert any(m["id"] == "dropped" for m in full["methods"])  # the omission is stated, not silent


def test_archived_spend_is_added_back_and_shown_both_ways(full):
    for rid in ("sweep-e1", "sweep-r2", "sweep-m1"):
        c = run_of(full, rid)["cost"]
        assert c["archived_added_back_usd"] > 0
        assert c["total_usd_with_archived"] == pytest.approx(c["total_usd_in_db"] + c["archived_added_back_usd"])
        assert c["per_pass_with_archived_usd"] > c["per_pass"]["usd"]
    assert run_of(full, "baseline")["cost"]["archived_added_back_usd"] == 0.0


def test_retry_windows_split_for_e1_only(full):
    e1 = run_of(full, "sweep-e1")["retry_windows"]
    assert set(e1) == {"9", "21"} or set(e1) == {9, 21}
    for rid in ("sweep-r2", "sweep-m1", "sweep-4"):
        w = run_of(full, rid)["code_versions"]
        assert set(map(str, w)) == {"9"}


def test_mcnemar_needs_table_matches_the_examples(full):
    needs = {row["discordant"]: row["needs_split_at_least"] for row in full["power"]["mcnemar_needs"]}
    # 5:0 is p = 0.0625 (not significant); 6:0 is p = 0.031; 7:1 is p = 0.070 (not significant); 8:1 is p = 0.039
    assert needs[5] is None and needs[6] == "6:0" and needs[8] == "8:0" and needs[9] == "8:1" and needs[10] == "9:1"
    assert S.mcnemar_exact(8, 1) < 0.05 < S.mcnemar_exact(7, 1)
    assert full["power"]["approx_minimum_detectable_gap_pp_two_independent_runs"] in range(36, 44)


# ------------------------------------------------------------------------------------------------ the artefacts
def _check(schema, value, path="$"):
    """Minimal structural check: required keys and array item requirements (no jsonschema dependency needed)."""
    errors = []
    t = schema.get("type")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key}")
        for key, sub in schema.get("properties", {}).items():
            if key in value and isinstance(sub, dict):
                errors += _check(sub, value[key], f"{path}.{key}")
    elif isinstance(value, list) and "items" in schema and isinstance(schema["items"], dict):
        for i, item in enumerate(value[:6]):
            errors += _check(schema["items"], item, f"{path}[{i}]")
    elif value is None and t not in (None, "null") and not (isinstance(t, list) and "null" in t):
        errors.append(f"{path}: null where {t}")
    return errors


def test_report_json_matches_the_schema(full, sample):
    schema = json.loads((BENCH / "site" / "data" / "report.schema.json").read_text())
    for rep in (full, sample):
        rep = json.loads(json.dumps(rep, default=float))
        assert _check(schema, rep) == []
    on_disk = json.loads((BENCH / "site" / "data" / "report.json").read_text())
    assert _check(schema, on_disk) == []
    assert [r["id"] for r in on_disk["runs"]] == [r["id"] for r in full["runs"]]


def test_disclosures_travel_with_the_report(full):
    text = " ".join(full["meta"]["disclosures"])
    for needle in ("filtered subset of SWE-bench Verified", "probably seen", "--no-pr", "hand-stated substitution", "AI Studio"):
        assert needle in text


def test_analyze_py_still_runs_unchanged(tmp_path):
    work = tmp_path / "BENCHMARK"
    shutil.copytree(BENCH / "data", work / "data")
    (work / "analysis").mkdir()
    shutil.copy(HERE / "analyze.py", work / "analysis" / "analyze.py")
    (work / "figures").mkdir()
    done = subprocess.run([sys.executable, "-I", str(work / "analysis" / "analyze.py")], capture_output=True, text=True, cwd=tmp_path, check=False)
    assert done.returncode == 0, done.stderr[-500:]
    fresh = json.loads((work / "analysis" / "results.json").read_text())
    old = json.loads((HERE / "results.json").read_text())
    assert fresh["headlines"] == old["headlines"]
    assert fresh["clustering"] == old["clustering"]
