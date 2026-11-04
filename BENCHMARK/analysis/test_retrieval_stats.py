"""Tests for BENCHMARK/analysis/retrieval_stats.py: determinism, Holm, the switch rule, power, and the output schema.

Uses the SYNTHETIC fixture (fixtures/retrieval_eval_fixture.json); nothing here is a measurement.

    uv run --all-packages pytest BENCHMARK/analysis/test_retrieval_stats.py -q
"""

import copy
import json
import math
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import retrieval_stats as R  # noqa: E402  (the module under test; the analysis directory is not a package)

FIXTURE = HERE / "fixtures" / "retrieval_eval_fixture.json"
SCHEMA = HERE.parents[1] / "api" / "repolace_api" / "site" / "retrieval_site" / "data" / "retrieval.schema.json"
SITE_JSON = SCHEMA.with_name("retrieval.json")


@pytest.fixture(autouse=True)
def small_resamples(monkeypatch):
    monkeypatch.setattr(R, "BOOT", 600)
    monkeypatch.setattr(R, "PERM", 800)


@pytest.fixture(scope="module")
def raw():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def built(raw):
    return R.build(raw, None, source="fixture")


def test_fixture_has_the_real_run_shape(raw):
    assert len(raw["strategies"]) * len(raw["query_strategies"]) == 8
    assert len({r["instance_id"] for r in raw["results"]}) == 18
    assert len(raw["results"]) == 18 * 8
    per_repo = {}
    for r in raw["results"]:
        if (r["strategy"], r["query_strategy"]) == R.BASELINE:
            per_repo[r["repo"]] = per_repo.get(r["repo"], 0) + 1
    assert sorted(per_repo.values()) == [1, 2, 3, 6, 6]
    assert "_synthetic" in raw


def test_bootstrap_is_deterministic_and_resamples_instances():
    vals = [0.0, 0.0, 0.5, 1.0, 0.2, 0.0, 0.1]
    a = R.bootstrap_mean(vals, "x")
    assert a == R.bootstrap_mean(vals, "x")
    assert a != R.bootstrap_mean(vals, "y")
    assert a[0] <= sum(vals) / len(vals) <= a[1]
    assert R.bootstrap_mean([0.3], "x") is None
    assert R.bootstrap_mean([0.4, 0.4, 0.4], "x") == pytest.approx([0.4, 0.4])


def test_two_builds_are_identical_except_the_timestamp(raw):
    a, b = R.build(raw, None), R.build(raw, None)
    a["meta"].pop("generated_at"), b["meta"].pop("generated_at")
    assert a == b


def test_holm_matches_the_textbook_example():
    assert R.S.holm([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert R.S.holm([0.9, 0.9]) == [1.0, 1.0]
    assert R.S.holm([0.001, 0.5]) == pytest.approx([0.002, 0.5])


def test_holm_adjusted_p_is_never_below_raw_and_at_most_one(built):
    for metric in built["comparisons"].values():
        for row in metric["rows"]:
            assert row["p"] <= row["p_holm"] <= 1.0


def test_sign_flip_p_is_small_for_a_consistent_gain_and_one_for_nothing():
    assert R.sign_flip_p([0.1] * 12, "a", 4000) < 0.01
    assert R.sign_flip_p([0.0] * 5, "a", 100) == 1.0
    assert R.sign_flip_p([0.1, -0.1, 0.1, -0.1], "a", 2000) > 0.5


def test_switch_rule_needs_the_size_and_the_interval(raw):
    # A cell that gains +30 pp on every instance meets the rule; one that gains +30 on average with a spread-out interval does not.
    big = copy.deepcopy(raw)
    base = {r["instance_id"]: r for r in big["results"] if (r["strategy"], r["query_strategy"]) == R.BASELINE}
    for r in big["results"]:
        if (r["strategy"], r["query_strategy"]) == ("windows", "truncate"):
            b = base[r["instance_id"]]["metrics"]["chunk_rr"]
            r["metrics"]["chunk_rr"] = None if b is None else min(1.0, b + 0.3) if b < 0.7 else b
    out = R.build(big, None)
    row = next(r for r in out["comparisons"]["chunk_mrr"]["rows"] if r["cell"] == "windows / truncate")
    assert row["mean_pp"] > 10 and row["ci_pp"][0] > 0 and row["meets_switch_rule"]
    assert out["switch_rule"]["any_met"] and out["switch_rule"]["met"] == ["windows / truncate"]
    # Only the primary metric can meet the rule.
    assert not any(r["meets_switch_rule"] for r in out["comparisons"]["file_mrr"]["rows"])


def test_the_fixture_is_a_null_result_like_the_real_one(built):
    assert built["synthetic"] is True
    assert built["switch_rule"]["any_met"] is False
    assert built["switch_rule"]["default_stays"] == "truncate / truncate"


def test_switch_rule_edge_cases(raw):
    # exactly the right size but an interval that includes zero: not met
    out = R.build(raw, None)
    for row in out["comparisons"]["chunk_mrr"]["rows"]:
        assert row["meets_switch_rule"] == (row["mean_pp"] >= 10 and row["ci_pp"][0] > 0)


def test_scored_counts_differ_by_metric_and_are_reported(built):
    cell = built["cells"][0]["metrics"]
    assert cell["chunk_mrr"]["scored"] == 17 and cell["chunk_mrr"]["skipped"] == 1
    assert cell["file_mrr"]["scored"] == 18
    assert all(c["chunk_scored"] == 17 and c["file_scored"] == 18 for c in built["denominators"]["cells"])
    assert built["comparisons"]["chunk_mrr"]["rows"][0]["paired"] == 17


def test_paired_difference_uses_only_instances_scored_in_both(built):
    row = built["comparisons"]["chunk_mrr"]["rows"][0]
    assert len(row["diffs_pp"]) == row["paired"] == row["wins"] + row["losses"] + row["ties"]
    assert row["mean_pp"] == pytest.approx(sum(row["diffs_pp"].values()) / row["paired"])
    assert row["dz"] == pytest.approx(row["mean_pp"] / row["sd_pp"])


def test_mde_and_power_agree_and_fall_with_n():
    nd = 18
    m = R.mde_pp(30.0, nd)
    assert m == pytest.approx(2.8016 * 30 / math.sqrt(nd), rel=1e-3)
    assert R.power_at(m, 30.0, nd) == pytest.approx(0.8, abs=0.005)
    assert R.power_at(0.0, 30.0, nd) == pytest.approx(0.05, abs=1e-6)
    assert R.mde_pp(30.0, 72) == pytest.approx(m / 2)
    assert R.mde_pp(30.0, nd, 0.05 / 7) > m


def test_power_block_curves_are_monotone(built):
    for c in built["power"]["curves"]:
        ys = [p[1] for p in c["points"]]
        assert ys == sorted(ys) and ys[0] == pytest.approx(0.05, abs=1e-6)


def test_rank_bins_cover_every_value():
    bins = R.rank_bins([1.0, 0.5, 1 / 3, 0.2, 0.1, 1 / 11, 0.05, 0.0])
    assert bins == {"1": 1, "2": 1, "3-5": 2, "6-10": 1, "11-20": 2, "miss": 1}


def test_heatmap_and_repo_blocks(built):
    assert len(built["heatmap"]["rows"]) == 18 and len(built["heatmap"]["cells"]) == 8
    repos = {r["repo"]: r["instances"] for r in built["repos"]["repos"]}
    assert repos["pytest-dev/pytest"] == repos["sphinx-doc/sphinx"] == 6
    assert built["repos"]["top_two_instances"] == 12 and built["repos"]["total_instances"] == 18
    assert built["repos"]["description_only"] is True


def test_unreachable_hunks_are_summed_once_per_instance(built, raw):
    per = {}
    for r in raw["results"]:
        per[r["instance_id"]] = r["metrics"]["unreachable_hunks"]
    assert built["unreachable"]["total_hunks"] == sum(per.values())
    assert built["unreachable"]["same_in_every_cell"] is True


def test_spearman_closed_forms():
    assert R.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert R.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert R.spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None
    assert R.spearman([1, 2], [1, 2]) is None


def test_sweep_link_excludes_harness_losses(raw, tmp_path):
    ids = sorted({r["instance_id"] for r in raw["results"]})
    rows = ["task_id,run,instance_id,run_index,status,outcome"]
    for n, iid in enumerate(ids):
        rows.append(f"a{n},sweep-4,{iid},0,completed,{'passed' if n % 2 else 'failed'}")
        rows.append(f"b{n},sweep-4,{iid},1,failed,")
        rows.append(f"c{n},sweep-3,{iid},0,completed,passed")
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text("\n".join(rows) + "\n")
    link = R.build(raw, csv_path)["sweep_link"]
    assert link["available"] and link["rows_total"] == 36 and link["rows_used"] == 18 and link["rows_excluded_harness_loss"] == 18
    assert all(p["runs"] == 1 for p in link["per_instance"])
    assert link["exploratory"] is True and link["spearman"]["n"] == 17


def test_sweep_link_missing_file_is_reported_not_raised(raw, tmp_path):
    link = R.build(raw, tmp_path / "nope.csv")["sweep_link"]
    assert link["available"] is False


def _validate(instance):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(instance, schema)


def test_ready_output_matches_the_schema(built):
    _validate(built)


def test_placeholder_matches_the_schema_and_is_not_synthetic():
    out = R.placeholder()
    assert out["status"] == "awaiting" and out["synthetic"] is False
    _validate(out)
    assert len(out["aggregate"]["cells"]) == 8 and len(out["aggregate"]["deltas"]) == 7
    best = next(d for d in out["aggregate"]["deltas"] if d["cell"] == "signature_docstring / truncate")
    assert best["chunk_mrr_pp"] == [5.3, 0.0, 14.8] and best["meets_switch_rule"] is False


def test_the_committed_page_data_is_never_synthetic():
    committed = json.loads(SITE_JSON.read_text())
    assert committed["synthetic"] is False
    assert "_synthetic" not in json.dumps(committed)
    _validate(committed)
