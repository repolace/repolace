#!/usr/bin/env python3
"""Statistics for every benchmark run, from the CSVs `export_runs.py` wrote. Writes site/data/report.json.

One function treats every run the same way. Nothing here pools runs: the one combined figure, the hand-stated
substitution of sweep-4 and its sweep-4b re-run ("baseline"), is built row by row (a lost row is replaced by its
re-run), labelled as a substitution everywhere, and is never produced by `repolace-eval report`.

Run:  uv run --all-packages python BENCHMARK/analysis/stats.py [--sample]
(`--sample` writes a small report from sweep-4/4b only, for building the site before every run is analysed.)

Rules from `.claude/rules/benchmark.md` this file follows:
* headline = passed / planned, where planned is the manifest grid; every planned row that is not a pass is a non-pass;
  `passed_with_test_edit` is never a pass; passed / admissible is a labelled secondary only;
* intervals are percentile bootstrap over instances (runs of one instance are not independent), never over rows;
* model/variant comparisons use instances scored under both, `run_index` 0, discordant counts and the exact McNemar test;
* a run with unfinished rows is marked PARTIAL (n of m) and is never compared on a rate;
* cost and latency are censored by the $2 cap and the wall clock and are flagged as such.
"""

import csv
import json
import math
import random
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    from scipy import stats as sps
except ImportError:  # pragma: no cover - the uv environment has scipy through torch
    sys.exit("scipy is required: run this with `uv run --all-packages python`")

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
DATA = BENCH / "data"
SITE_DATA = BENCH / "site" / "data"

SEED = 20261025
BOOT = 20_000
PERM = 20_000
COST_CAP_USD = 2.0
AGENT_WALL_S = 3600.0
RUNNER_WALL_S = 11_700.0

#: run id -> report metadata. `baseline` is built, not exported.
META = {
    "sweep-4": {"label": "Single agent, Gemini 3.1 Pro", "short": "sweep-4", "kind": "baseline-official"},
    "sweep-4b": {"label": "Re-run of the 12 rows sweep-4 lost to 429", "short": "sweep-4b", "kind": "baseline-rerun"},
    "sweep-5": {"label": "Single agent, Gemini 3.8 Flash", "short": "sweep-5", "kind": "model"},
    "sweep-r1": {"label": "Role split, Gemini 3.1 Pro (first attempt)", "short": "r1", "kind": "variant"},
    "sweep-r2": {"label": "Role split, Gemini 3.1 Pro", "short": "r2", "kind": "variant"},
    "sweep-e1": {"label": "Early-edit nudge, Gemini 3.1 Pro", "short": "e1", "kind": "variant"},
    "sweep-m1": {"label": "Role split, Flash editor with Pro plan/review/debug", "short": "m1", "kind": "variant"},
}
VARIANTS = ["sweep-e1", "sweep-r2", "sweep-m1"]  # the family the pre-registered rule applies to, Holm-corrected
BUCKETS = ["unfinished", "harness_error", "inadmissible", "passed_with_test_edit", "passed", "failed"]


# --------------------------------------------------------------------------------------------------------- basics
def pct(x):
    return None if x is None else round(100 * x, 1)


def quantile(xs, q):
    """Linear-interpolation quantile of an already sorted list (type 7)."""
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def describe(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "mean": sum(xs) / len(xs), "min": xs[0], "p10": quantile(xs, 0.10), "median": quantile(xs, 0.5),
            "p90": quantile(xs, 0.90), "max": xs[-1]}


def wilson(k, n, z=1.959964):
    """Wilson score interval. Ignores clustering, so it is context only, never the headline interval."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [centre - half, centre + half]


def rng_for(label):
    """Deterministic stream per statistic. The baseline headline uses the bare seed, which reproduces the first
    bootstrap `analyze.py` ran (44.4 to 83.3)."""
    return random.Random(SEED) if label == "baseline" else random.Random(f"{SEED}/{label}")


def boot_ci(per_instance, rng, stat):
    """Percentile bootstrap over instances: resample whole instances with replacement, recompute `stat(sample)`."""
    ids = list(per_instance)
    vals = []
    for _ in range(BOOT):
        sample = [per_instance[rng.choice(ids)] for _ in ids]
        v = stat(sample)
        if v is not None:
            vals.append(v)
    vals.sort()
    return {"lo": quantile(vals, 0.025), "hi": quantile(vals, 0.975), "se": _sd(vals), "n_resamples": len(vals), "samples": vals}


def _sd(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def rate_of(sample):
    """Pooled pass rate of a bootstrap sample of (passes, planned) pairs."""
    n = sum(p[1] for p in sample)
    return sum(p[0] for p in sample) / n if n else None


def pass_at_k(c, n, k):
    """Unbiased estimator of P(at least one of k draws passes) from c passes in n runs."""
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def icc_oneway(groups):
    """ICC(1) by one-way ANOVA for equal-sized groups of 0/1 outcomes (runs of one instance)."""
    k, m = len(groups), len(groups[0])
    grand = sum(sum(g) for g in groups) / (k * m)
    msb = m * sum((sum(g) / m - grand) ** 2 for g in groups) / (k - 1)
    msw = sum(sum((x - sum(g) / m) ** 2 for x in g) for g in groups) / (k * (m - 1))
    den = msb + (m - 1) * msw
    return ((msb - msw) / den if den else 0.0), msb, msw


def cochran_q(matrix):
    """Cochran's Q over instances (rows) x run indexes (columns) of 0/1; p from the chi-square survival function."""
    k = len(matrix[0])
    col = [sum(r[j] for r in matrix) for j in range(k)]
    row = [sum(r) for r in matrix]
    total = sum(col)
    denom = k * total - sum(r * r for r in row)
    if denom == 0:
        return {"q": 0.0, "df": k - 1, "p": 1.0}
    q = (k - 1) * (k * sum(c * c for c in col) - total * total) / denom
    return {"q": q, "df": k - 1, "p": float(sps.chi2.sf(q, k - 1))}


def mcnemar_exact(b, c):
    """Exact two-sided McNemar: under H0 the discordant pairs split 50/50, so a binomial test on min(b, c) of b + c."""
    n = b + c
    if n == 0:
        return 1.0
    return float(sps.binomtest(min(b, c), n, 0.5, alternative="two-sided").pvalue)


def holm(pvals):
    """Holm-Bonferroni adjusted p-values, in the input order."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvals[i]))
        adj[i] = running
    return adj


def cohens_h(p1, p2):
    return 2 * math.asin(math.sqrt(p1)) - 2 * math.asin(math.sqrt(p2))


def auc_less(xs, ys):
    """P(x < y) + half the ties, for x from xs and y from ys (does the first group act earlier?)."""
    if not xs or not ys:
        return None
    wins = sum((x < y) + 0.5 * (x == y) for x in xs for y in ys)
    return wins / (len(xs) * len(ys))


def sign_flip_perm(diffs, rng):
    """Two-sided sign-flip permutation test of mean(diffs) = 0 (paired data, exchangeable under H0)."""
    obs = sum(diffs)
    hits = 0
    for _ in range(PERM):
        s = sum(d if rng.random() < 0.5 else -d for d in diffs)
        if abs(s) >= abs(obs) - 1e-12:
            hits += 1
    return (hits + 1) / (PERM + 1)


def mantel_haenszel(strata):
    """Mantel-Haenszel common odds ratio over 2x2 tables (a=variant pass, b=variant fail, c=base pass, d=base fail),
    with the Robins-Breslow-Greenland variance for the log OR. Strata with no variation in both rows and columns carry
    no information and are dropped; with fewer than two informative strata the estimate is reported as undefined."""
    informative = []
    for a, b, c, d in strata:
        n = a + b + c + d
        if n == 0 or (a + c) in (0, n) or (a + b) in (0, n):
            continue
        informative.append((a, b, c, d))
    if len(informative) < 2:
        return {"defined": False, "informative_strata": len(informative), "strata": len(strata),
                "reason": "fewer than two instances where both the condition and the outcome vary"}
    r_num = sum(a * d / (a + b + c + d) for a, b, c, d in informative)
    s_den = sum(b * c / (a + b + c + d) for a, b, c, d in informative)
    if s_den == 0 or r_num == 0:
        return {"defined": False, "informative_strata": len(informative), "strata": len(strata),
                "reason": "a zero cell total makes the odds ratio zero or infinite"}
    or_mh = r_num / s_den
    p_ = [(a + d) / (a + b + c + d) for a, b, c, d in informative]
    q_ = [(b + c) / (a + b + c + d) for a, b, c, d in informative]
    r_ = [a * d / (a + b + c + d) for a, b, c, d in informative]
    s_ = [b * c / (a + b + c + d) for a, b, c, d in informative]
    var = (sum(pp * rr for pp, rr in zip(p_, r_, strict=True)) / (2 * r_num ** 2)
           + sum(pp * ss + qq * rr for pp, qq, rr, ss in zip(p_, q_, r_, s_, strict=True)) / (2 * r_num * s_den)
           + sum(qq * ss for qq, ss in zip(q_, s_, strict=True)) / (2 * s_den ** 2))
    se = math.sqrt(var)
    lo, hi = math.exp(math.log(or_mh) - 1.959964 * se), math.exp(math.log(or_mh) + 1.959964 * se)
    return {"defined": True, "odds_ratio": or_mh, "ci": [lo, hi], "informative_strata": len(informative),
            "strata": len(strata)}


# ------------------------------------------------------------------------------------------------------- loading
def _f(x):
    return float(x) if x not in ("", None) else None


def _i(x):
    return int(x) if x not in ("", None) else None


def read_csv(path):
    csv.field_size_limit(10**9)
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def repo_of(instance_id):
    return instance_id.split("__")[0].replace("-dev", "").replace("-doc", "")


def load_run(run):
    """Everything about one run as plain dicts. A missing directory means the run was not exported."""
    d = DATA / run
    manifest = json.loads((d / "manifest.json").read_text()) if (d / "manifest.json").exists() else {}
    tasks = read_csv(d / "tasks.csv")
    stage = read_csv(d / "llm_stage.csv")
    tools = {r["task_id"]: r for r in read_csv(d / "tool_use.csv")}
    calls = read_csv(d / "calls.csv")
    cost, tokens = {}, {}
    for r in stage:
        t = cost.setdefault(r["task_id"], {"cost": 0.0, "calls": 0, "agent_calls": 0})
        t["cost"] += float(r["cost_usd"] or 0)
        t["calls"] += int(r["calls"])
        if r["stage"] == "agent":
            t["agent_calls"] += int(r["calls"])
        k = tokens.setdefault(r["task_id"], [0, 0, 0])
        k[0] += int(r["input_tokens"])
        k[1] += int(r["cached_input_tokens"])
        k[2] += int(r["output_tokens"])
    rows = []
    for t in tasks:
        tid = t["task_id"]
        tu = tools.get(tid, {})
        rows.append({
            "task_id": tid, "run": run, "instance_id": t["instance_id"], "run_index": int(t["run_index"]),
            "status": t["status"], "outcome": t["outcome"] or None, "stop_reason": t["stop_reason"] or None,
            "cost": cost.get(tid, {}).get("cost", 0.0), "calls": cost.get(tid, {}).get("calls", 0),
            "agent_calls": cost.get(tid, {}).get("agent_calls", 0),
            "input_tokens": tokens.get(tid, [0, 0, 0])[0], "cached_tokens": tokens.get(tid, [0, 0, 0])[1],
            "output_tokens": tokens.get(tid, [0, 0, 0])[2],
            "latency_s": _f(t["latency_s"]), "started_at": t["started_at"] or None,
            "scored_attempts": _i(t["scored_attempts"]) or 0, "error_head": t["error_head"] or None,
            "first_edit": _i(tu.get("first_edit_call")), "edit_calls": _i(tu.get("edit_calls")) or 0,
            "run_calls": _i(tu.get("run_calls")) or 0, "read_calls": _i(tu.get("read_calls")) or 0,
            "submit_calls": _i(tu.get("submit_calls")) or 0, "tool_calls": _i(tu.get("tool_calls")) or 0,
        })
    return {"id": run, "manifest": manifest, "rows": rows, "calls": calls}


def planned_grid(manifest, rows):
    """The cells the run planned: the manifest's instance x run grid, or its `rows` list when it names a subset."""
    if manifest.get("rows"):
        return sorted((r.split("#")[0], int(r.split("#")[1])) for r in manifest["rows"])
    ids = manifest.get("instance_ids") or sorted({r["instance_id"] for r in rows})
    per = manifest.get("runs_per_instance") or 3
    return sorted((i, k) for i in ids for k in range(per))


def bucket(row):
    """First match wins, per benchmark.md. A missing row is unfinished."""
    if row is None or row["status"] in ("queued", "running"):
        return "unfinished"
    if row["status"] == "failed":
        return "harness_error"
    if row["outcome"] is None:
        return "inadmissible"
    if row["outcome"] == "passed_with_test_edit":
        return "passed_with_test_edit"
    return row["outcome"] if row["outcome"] in ("passed", "failed") else "inadmissible"


def passed(row):
    return row is not None and row["status"] == "completed" and row["outcome"] == "passed"


# ------------------------------------------------------------------------------------------- per-run statistics
def build_grid(run_data):
    """cell -> row (or None) for the planned grid."""
    by = {(r["instance_id"], r["run_index"]): r for r in run_data["rows"]}
    return {cell: by.get(cell) for cell in planned_grid(run_data["manifest"], run_data["rows"])}


def analyse(label, meta, grid, manifest, code_versions, archived_usd, extra=None):
    """The same treatment for every run, from its planned grid (cell -> row or None)."""
    cells = sorted(grid)
    instances = sorted({c[0] for c in cells})
    rows = [grid[c] for c in cells]
    buckets = dict.fromkeys(BUCKETS, 0)
    for r in rows:
        buckets[bucket(r)] += 1
    planned = len(cells)
    n_pass = sum(passed(r) for r in rows)
    finished = planned - buckets["unfinished"]
    partial = buckets["unfinished"] > 0
    per_inst = {i: (sum(passed(grid[c]) for c in cells if c[0] == i), sum(1 for c in cells if c[0] == i)) for i in instances}
    ci = boot_ci(per_inst, rng_for(label), rate_of)
    admissible = [r for r in rows if r and r["status"] == "completed" and r["outcome"] in ("passed", "failed")]
    out = {
        "id": label, **meta, "planned": planned, "finished": finished, "passed": n_pass, "partial": partial,
        "partial_mark": f"PARTIAL ({finished} of {planned})" if partial else None,
        "buckets": buckets,
        "headline": {
            "passed": n_pass, "planned": planned, "rate": n_pass / planned if planned else None,
            "ci_bootstrap_instances": [ci["lo"], ci["hi"]], "se": ci["se"], "n_resamples": ci["n_resamples"],
            "wilson_rows_context_only": wilson(n_pass, planned),
            "definition": "passed / planned; every planned row that is not a pass counts as a non-pass",
        },
        "secondary_admissible": {
            "passed": sum(passed(r) for r in admissible), "admissible": len(admissible),
            "rate": (sum(passed(r) for r in admissible) / len(admissible)) if admissible else None,
            "wilson_rows_context_only": wilson(sum(passed(r) for r in admissible), len(admissible)),
            "note": "excludes harness errors and unfinished rows; a labelled secondary, never the headline",
        },
        "bootstrap_samples": [round(v, 5) for v in ci["samples"][::40]],
    }
    full_instances = [i for i in instances if per_inst[i][1] == 3]
    if len(full_instances) == len(instances) and not partial and len(instances) > 2:
        groups = [[1 if passed(grid[(i, k)]) else 0 for k in range(3)] for i in instances]
        icc, _msb, _msw = icc_oneway(groups)
        deff = 1 + 2 * icc
        out["clustering"] = {"icc_oneway": icc, "design_effect_3_runs": deff, "effective_n_rows": planned / deff,
                             "cochran_q_run_index": cochran_q(groups),
                             "reading": "runs of one instance are correlated, so the rows carry less evidence than their count"}
        out["pass_at_k"] = {
            "pass_at_1": sum(sum(g) / 3 for g in groups) / len(groups),
            "pass_at_3": sum(pass_at_k(sum(g), 3, 3) for g in groups) / len(groups),
        }
        out["per_run_index"] = [sum(g[k] for g in groups) / len(groups) for k in range(3)]
    done = [r for r in rows if r and r["status"] == "completed"]
    stops = {}
    for r in done:
        s = stops.setdefault(r["stop_reason"] or "none", {"n": 0, "passed": 0})
        s["n"] += 1
        s["passed"] += passed(r)
    out["stop_reasons"] = stops
    out["stop_reason_shares_of_completed"] = {k: v["n"] / len(done) for k, v in stops.items()} if done else {}
    edits = sorted(r["first_edit"] for r in done if r["first_edit"] is not None)
    out["behaviour"] = {
        "completed_rows": len(done),
        "step_cap_share": (stops.get("step_cap", {"n": 0})["n"] / len(done)) if done else None,
        "submitted_share": (stops.get("submitted", {"n": 0})["n"] / len(done)) if done else None,
        "no_change_share": (stops.get("no_change", {"n": 0})["n"] / len(done)) if done else None,
        "first_edit_call": describe(edits),
        "never_edited": sum(1 for r in done if r["first_edit"] is None),
        "first_edit_passed": describe([r["first_edit"] for r in done if passed(r) and r["first_edit"] is not None]),
        "first_edit_failed": describe([r["first_edit"] for r in done if not passed(r) and r["first_edit"] is not None]),
        "first_edit_ecdf": [[v, (i + 1) / len(edits)] for i, v in enumerate(edits)] if edits else [],
        "auc_passed_edits_earlier": auc_less([r["first_edit"] for r in done if passed(r) and r["first_edit"] is not None],
                                              [r["first_edit"] for r in done if not passed(r) and r["first_edit"] is not None]),
        "tool_mix": _tool_mix(done),
    }
    costs = sorted(r["cost"] for r in done)
    lat = sorted(r["latency_s"] for r in done if r["latency_s"] is not None)
    in_tok, cached, out_tok = (sum(r[k] for r in done) for k in ("input_tokens", "cached_tokens", "output_tokens"))
    cost_total = sum(r["cost"] for r in done)
    cp = None
    if n_pass:
        pi_cost = {i: (sum((grid[c]["cost"] if grid[c] else 0.0) for c in cells if c[0] == i),
                       sum(passed(grid[c]) for c in cells if c[0] == i)) for i in instances}
        cpp = boot_ci(pi_cost, rng_for(label + "/cost-per-pass"),
                      lambda s: (sum(x[0] for x in s) / sum(x[1] for x in s)) if sum(x[1] for x in s) else None)
        cp = {"usd": sum(grid[c]["cost"] for c in cells if grid[c]) / n_pass, "ci_bootstrap_instances": [cpp["lo"], cpp["hi"]]}
    out["cost"] = {
        "total_usd_in_db": cost_total, "archived_added_back_usd": archived_usd,
        "total_usd_with_archived": cost_total + archived_usd,
        "per_task": describe(costs), "per_pass": cp,
        "per_pass_with_archived_usd": ((cost_total + archived_usd) / n_pass) if n_pass else None,
        "rows_at_or_above_cap": sum(1 for r in done if r["cost"] >= COST_CAP_USD * 0.99),
        "censoring": "each task is capped at $2.00; per-task cost is a lower bound where the cap binds",
        "price_basis": "AI Studio list rates, not billed Vertex rates (priority rates came from a browser read)",
    }
    out["latency_s"] = {**describe(lat), "rows_near_agent_wall_clock": sum(1 for r in done if r["stop_reason"] == "budget_wall"),
                        "censoring": "agent wall clock is 3,600 s and a runner kills at 11,700 s; sweep contention is included"}
    out["tokens"] = {"input": in_tok, "cached_input": cached, "output": out_tok,
                     "cache_hit_share": (cached / in_tok) if in_tok else None, "calls": sum(r["calls"] for r in done)}
    out["lost_rows"] = [{"instance_id": c[0], "run_index": c[1], "bucket": bucket(grid[c]),
                         "error": (grid[c] or {}).get("error_head")} for c in cells
                        if bucket(grid[c]) in ("harness_error", "unfinished")]
    out["instances"] = [_instance_view(i, grid, cells) for i in instances]
    out["tasks"] = [_task_view(grid[c], c) for c in cells]
    out["power"] = {
        "instances": len(instances), "one_instance_moves_the_rate_pp": round(100 / len(instances), 1),
        "bootstrap_se_pp": round(ci["se"] * 100, 1) if ci["se"] else None,
    }
    out["code_versions"] = code_versions
    if manifest:
        out["manifest"] = {k: manifest.get(k) for k in ("git_sha", "created_at", "model", "roles", "stage_models", "limits",
                                                        "runs_per_instance")}
        out["manifest"]["n_instances"] = len(manifest.get("instance_ids") or [])
    if extra:
        out.update(extra)
    return out


def _tool_mix(done):
    tot = {"edit": 0, "run": 0, "read": 0, "submit": 0}
    for r in done:
        tot["edit"] += r["edit_calls"]
        tot["run"] += r["run_calls"]
        tot["read"] += r["read_calls"]
        tot["submit"] += r["submit_calls"]
    n = sum(tot.values())
    return {k: (v / n if n else None) for k, v in tot.items()} | {"tool_calls": n}


def _instance_view(i, grid, cells):
    mine = [c for c in cells if c[0] == i]
    return {
        "instance_id": i, "repo": repo_of(i), "planned": len(mine),
        "passes": sum(passed(grid[c]) for c in mine),
        "outcomes": [{"run_index": c[1], "bucket": bucket(grid[c]), "stop_reason": (grid[c] or {}).get("stop_reason")}
                     for c in mine],
        "cost_usd": sum((grid[c] or {}).get("cost", 0.0) for c in mine),
    }


def _task_view(row, cell):
    base = {"instance_id": cell[0], "run_index": cell[1], "bucket": bucket(row)}
    if row is None:
        return base
    return base | {"stop_reason": row["stop_reason"], "cost": round(row["cost"], 4), "latency_s": row["latency_s"],
                   "first_edit": row["first_edit"], "scored_attempts": row["scored_attempts"], "calls": row["calls"],
                   "started_at": row["started_at"]}


def substitution(s4, s4b):
    """Hand-stated substitution: each sweep-4 row that is not completed is replaced by its sweep-4b re-run. Not pooling:
    every cell holds exactly one of the two rows, and the report says which cells were substituted."""
    cells = build_grid(s4)
    rerun = {(r["instance_id"], r["run_index"]): r for r in s4b["rows"]}
    sub = []
    for c, r in cells.items():
        if (r is None or r["status"] != "completed") and c in rerun and rerun[c]["status"] == "completed":
            cells[c] = rerun[c]
            sub.append(c)
    return cells, sub


# ------------------------------------------------------------------------------------------------ comparisons
def baseline_view(grid):
    return {c: ("pass" if passed(r) else ("fail" if r and r["status"] == "completed" else None)) for c, r in grid.items()}


def secondary_metrics(done):
    cap = sum(1 for r in done if r["stop_reason"] == "step_cap")
    sub = sum(1 for r in done if r["stop_reason"] == "submitted")
    edits = sorted(r["first_edit"] for r in done if r["first_edit"] is not None)
    return {"n": len(done), "median_first_edit_call": quantile(edits, 0.5), "step_cap_share": cap / len(done) if done else None,
            "median_cost_per_task_usd": quantile(sorted(r["cost"] for r in done), 0.5),
            "mean_cost_per_task_usd": sum(r["cost"] for r in done) / len(done) if done else None,
            "submitted_share": sub / len(done) if done else None}


def compare(variant_id, variant_grid, base_grid, label, se_variant, se_base):
    """Variant against the baseline substitution. The primary block is the pre-registered rule; `exploratory` is context."""
    rng_base = f"{label}"
    v, b = baseline_view(variant_grid), baseline_view(base_grid)
    shared0 = [c for c in sorted(base_grid) if c[1] == 0 and v.get(c) and b.get(c)]
    bb = sum(1 for c in shared0 if b[c] == "pass" and v[c] == "fail")  # baseline only
    cc = sum(1 for c in shared0 if b[c] == "fail" and v[c] == "pass")  # variant only
    both_pass = sum(1 for c in shared0 if b[c] == "pass" and v[c] == "pass")
    both_fail = sum(1 for c in shared0 if b[c] == "fail" and v[c] == "fail")
    p_mc = mcnemar_exact(bb, cc)
    v_done = [r for r in variant_grid.values() if r and r["status"] == "completed"]
    b_done = [r for r in base_grid.values() if r and r["status"] == "completed"]
    var_rate = sum(passed(r) for r in variant_grid.values()) / len(variant_grid)
    base_rate = sum(passed(r) for r in base_grid.values()) / len(base_grid)
    primary = {
        "rule": "Fixed before any data: pass rate vs the 35/54 baseline substitution with the bootstrap over instances; "
                "secondary: median first-edit step, step-cap share, cost per task, submitted share. McNemar is exact, on "
                "run_index 0, over the instances scored in both runs; discordant counts are shown, never a rate difference alone.",
        "mcnemar_run_index_0": {"pairs": len(shared0), "baseline_only_passed": bb, "variant_only_passed": cc,
                                "both_passed": both_pass, "both_failed": both_fail, "p_exact_two_sided": p_mc},
        "secondary_metrics": {"variant": secondary_metrics(v_done), "baseline": secondary_metrics(b_done)},
        "baseline_inside_variant_interval": None,
    }
    ids = sorted({c[0] for c in base_grid})
    pv = {i: sum(passed(variant_grid.get((i, k))) for k in range(3)) for i in ids}
    pb = {i: sum(passed(base_grid.get((i, k))) for k in range(3)) for i in ids}
    diffs = [pv[i] - pb[i] for i in ids]
    nz = [d for d in diffs if d != 0]
    pos, neg = sum(d > 0 for d in nz), sum(d < 0 for d in nz)
    sign_p = float(sps.binomtest(min(pos, neg), len(nz), 0.5).pvalue) if nz else 1.0
    try:
        wil = sps.wilcoxon(diffs, zero_method="wilcox", alternative="two-sided", method="auto") if nz else None
        wil_p = float(wil.pvalue) if wil is not None else 1.0
    except ValueError:
        wil_p = None
    paired = {i: (pv[i] - pb[i], 3) for i in ids}
    dci = boot_ci(paired, rng_for(rng_base + "/paired"), lambda s: sum(x[0] for x in s) / sum(x[1] for x in s))
    strata = [(pv[i], 3 - pv[i], pb[i], 3 - pb[i]) for i in ids]
    flips = [{"instance_id": i, "baseline_passes": pb[i], "variant_passes": pv[i]} for i in ids if pv[i] != pb[i]]
    exploratory = {
        "label": "Exploratory context. benchmark.md says a comparison is never a rate difference alone; these do not change the verdict.",
        "rate_difference": var_rate - base_rate,
        "paired_cluster_bootstrap_difference": {"estimate": (sum(diffs) / (3 * len(ids))), "ci": [dci["lo"], dci["hi"]],
                                                "note": "resamples whole instances; the paired differences of passes out of 3"},
        "sign_test": {"instances_better": pos, "instances_worse": neg, "tied": len(diffs) - len(nz), "p_exact_two_sided": sign_p},
        "wilcoxon_signed_rank": {"p": wil_p, "n_nonzero": len(nz), "note": "ties broken by the Wilcoxon zero method; exact for small n"},
        "permutation_sign_flip": {"p": sign_flip_perm(diffs, rng_for(rng_base + "/perm")) if nz else 1.0, "n_perm": PERM},
        "cohens_h": cohens_h(var_rate, base_rate),
        "mantel_haenszel": mantel_haenszel(strata),
        "flips": flips,
        "minimum_detectable_gap_pp": round(2.8 * math.sqrt(se_variant ** 2 + se_base ** 2) * 100) if se_variant and se_base else None,
    }
    return {"variant": variant_id, "baseline": "baseline", "n_instances": len(ids),
            "variant_rate": var_rate, "baseline_rate": base_rate,
            "primary": primary, "exploratory": exploratory}


def mcnemar_split_table(n_pairs):
    """For n discordant pairs, the least lopsided split (majority:minority) that is still significant at 0.05."""
    out = []
    for n in range(1, n_pairs + 1):
        sig = [k for k in range(n // 2 + 1) if mcnemar_exact(k, n - k) < 0.05]
        least = max(sig) if sig else None  # the least lopsided split that is still significant
        out.append({"discordant": n, "needs_split_at_least": None if least is None else f"{n - least}:{least}"})
    return out


# ------------------------------------------------------------------------------------------------------ driver
def load_inputs():
    cv_path = SITE_DATA / "code_versions.json"
    cv = json.loads(cv_path.read_text()) if cv_path.exists() else {}
    arch = json.loads((DATA / "archived_spend.json").read_text()) if (DATA / "archived_spend.json").exists() else {}
    retrieval = json.loads((DATA / "retrieval.json").read_text()) if (DATA / "retrieval.json").exists() else None
    return cv, arch, retrieval


def retry_window_counts(cv, run):
    return (cv.get("rows_by_retry_window") or {}).get(run, {})


def build_report(sample=False):
    cv, arch, retrieval = load_inputs()
    run_ids = ["sweep-4", "sweep-4b"] if sample else list(META)
    loaded = {r: load_run(r) for r in run_ids if (DATA / r / "tasks.csv").exists()}
    runs = []
    # --- baseline pair first
    s4g = build_grid(loaded["sweep-4"])
    runs.append(analyse("sweep-4", META["sweep-4"], s4g, loaded["sweep-4"]["manifest"], retry_window_counts(cv, "sweep-4"), 0.0,
                        {"headline_note": "Official sweep-4 headline: 12 of the 54 rows were lost to Vertex 429 and count as non-passes."}))
    b4g = build_grid(loaded["sweep-4b"])
    runs.append(analyse("sweep-4b", META["sweep-4b"], b4g, loaded["sweep-4b"]["manifest"], retry_window_counts(cv, "sweep-4b"), 0.0,
                        {"headline_note": "A subset: only the rows sweep-4 lost. Its rate is not comparable to a full run."}))
    base_grid, substituted = substitution(loaded["sweep-4"], loaded["sweep-4b"])
    base = analyse("baseline", {"label": "Baseline: sweep-4 with its 12 lost rows replaced by their sweep-4b re-runs",
                                "short": "baseline", "kind": "baseline-substitution"},
                   base_grid, loaded["sweep-4"]["manifest"], retry_window_counts(cv, "sweep-4"), 0.0,
                   {"substitution": {"hand_stated": True, "substituted_cells": [f"{c[0]}#{c[1]}" for c in substituted],
                                     "statement": "Hand-stated substitution of sweep-4 and sweep-4b, not a pooled run and not produced by "
                                                  "`repolace-eval report`. Each of the 54 cells holds exactly one row."}})
    runs.append(base)
    comparisons, grids = [], {"baseline": base_grid}
    for rid in [r for r in run_ids if r not in ("sweep-4", "sweep-4b")]:
        if rid not in loaded:
            continue
        g = build_grid(loaded[rid])
        grids[rid] = g
        add = (arch.get(rid, {}) or {}).get("added_back_usd", 0.0)
        extra = {"retry_windows": retry_window_counts(cv, rid)}
        if rid in arch:
            extra["archived_attempts"] = {k: arch[rid][k] for k in ("generation_1", "generation_2", "added_back_usd")}
        runs.append(analyse(rid, META[rid], g, loaded[rid]["manifest"], retry_window_counts(cv, rid), add, extra))
    if not sample:
        full = [r for r in runs if r["id"] in VARIANTS and not r["partial"]]
        raw = []
        for r in full:
            c = compare(r["id"], grids[r["id"]], base_grid, f"cmp/{r['id']}", r["headline"]["se"], base["headline"]["se"])
            comparisons.append(c)
            raw.append(c["primary"]["mcnemar_run_index_0"]["p_exact_two_sided"])
        for c, adj in zip(comparisons, holm(raw), strict=True):
            c["primary"]["mcnemar_run_index_0"]["p_holm_adjusted"] = adj
            c["primary"]["holm_family"] = VARIANTS
        sign_adj = holm([c["exploratory"]["sign_test"]["p_exact_two_sided"] for c in comparisons])
        for c, a in zip(comparisons, sign_adj, strict=True):
            c["exploratory"]["sign_test"]["p_holm_adjusted"] = a
        # sweep-5 is partial: McNemar only on run_index 0 pairs scored in both, no rate comparison
        if "sweep-5" in grids:
            comparisons.append(partial_comparison("sweep-5", grids["sweep-5"], base_grid))
        for c in comparisons:
            if c.get("partial"):
                continue
            r = next(x for x in runs if x["id"] == c["variant"])
            lo, hi = r["headline"]["ci_bootstrap_instances"]
            c["primary"]["baseline_inside_variant_interval"] = bool(lo <= c["baseline_rate"] <= hi)
    difficulty = build_difficulty(runs, grids) if not sample else None
    return {
        "meta": meta_block(sample),
        "runs": runs,
        "comparisons": comparisons,
        "difficulty": difficulty,
        "cost_effectiveness": cost_effectiveness(runs) if not sample else None,
        "cost_curves": cost_curves(loaded, grids) if not sample else None,
        "failure_flow": failure_flow(runs),
        "retrieval": retrieval_view(retrieval) if not sample else None,
        "power": power_block(base),
        "methods": METHODS,
        "code_versions": {"source": "site/data/code_versions.json", "baseline_claim_check": cv.get("baseline_claim_check"),
                          "e1_retry_window_commit": cv.get("e1_retry_window_commit")},
    }


def partial_comparison(rid, grid, base_grid):
    v, b = baseline_view(grid), baseline_view(base_grid)
    shared0 = [c for c in sorted(base_grid) if c[1] == 0 and v.get(c) and b.get(c)]
    bb = sum(1 for c in shared0 if b[c] == "pass" and v[c] == "fail")
    cc = sum(1 for c in shared0 if b[c] == "fail" and v[c] == "pass")
    return {"variant": rid, "baseline": "baseline", "n_instances": len(shared0), "partial": True,
            "variant_rate": None, "baseline_rate": None,
            "note": "Partial run: only run_index 0 pairs scored in both runs are compared, as discordant counts and the exact "
                    "McNemar p. No rate is compared (benchmark.md).",
            "primary": {"mcnemar_run_index_0": {"pairs": len(shared0), "baseline_only_passed": bb, "variant_only_passed": cc,
                                                "both_passed": sum(1 for c in shared0 if b[c] == "pass" and v[c] == "pass"),
                                                "both_failed": sum(1 for c in shared0 if b[c] == "fail" and v[c] == "fail"),
                                                "p_exact_two_sided": mcnemar_exact(bb, cc)},
                        "holm_family": None, "baseline_inside_variant_interval": None},
            "exploratory": None}


def build_difficulty(runs, grids):
    full = [r for r in ("baseline", "sweep-e1", "sweep-r2", "sweep-m1") if r in grids]
    ids = sorted({c[0] for c in grids["baseline"]})
    table = []
    for i in ids:
        per = {r: sum(passed(grids[r].get((i, k))) for k in range(3)) for r in full}
        total = sum(per.values())
        n = 3 * len(full)
        table.append({"instance_id": i, "repo": repo_of(i), "passes_by_run": per, "passes": total, "of": n,
                      "class": "always solved" if total == n else ("never solved" if total == 0 else "sometimes solved")})
    solved = {r: {i for i in ids if sum(passed(grids[r].get((i, k))) for k in range(3)) >= 1} for r in full}
    jac = {a: {b: (len(solved[a] & solved[b]) / len(solved[a] | solved[b]) if (solved[a] | solved[b]) else None) for b in full}
           for a in full}
    return {"runs": full, "instances": table, "solved_at_least_once_sizes": {r: len(solved[r]) for r in full},
            "jaccard_solved_at_least_once": jac,
            "note": "Descriptive only. 'Class' is over the four full runs together (12 attempts), not a model of difficulty."}


def cost_effectiveness(runs):
    pts = []
    for r in runs:
        if r["id"] in ("sweep-4", "sweep-4b") or r["partial"]:
            continue
        c = r["cost"]
        pts.append({"id": r["id"], "label": r["label"], "pass_rate": r["headline"]["rate"],
                    "usd_per_task": c["total_usd_in_db"] / max(1, r["behaviour"]["completed_rows"]),
                    "usd_per_pass": c["per_pass"]["usd"] if c["per_pass"] else None,
                    "usd_per_pass_ci": c["per_pass"]["ci_bootstrap_instances"] if c["per_pass"] else None,
                    "usd_per_pass_with_archived": c["per_pass_with_archived_usd"]})
    for p in pts:
        p["on_frontier"] = not any(q is not p and q["pass_rate"] >= p["pass_rate"] and q["usd_per_task"] <= p["usd_per_task"]
                                   and (q["pass_rate"] > p["pass_rate"] or q["usd_per_task"] < p["usd_per_task"]) for q in pts)
    return {"points": pts, "note": "Frontier = no other full run has both a higher-or-equal pass rate and a lower-or-equal cost "
                                   "per task. Prices are AI Studio list rates; costs are censored by the $2 cap."}


def cost_curves(loaded, grids):
    """Mean cumulative cost by model-call index for each full run, over tasks that reached that index (n shown)."""
    out = {}
    for rid, run in loaded.items():
        if rid == "sweep-4b" or rid not in grids:
            continue
        by = {}
        for c in run["calls"]:
            by.setdefault(c["task_id"], []).append(c)
        series = []
        for cs in by.values():
            cs.sort(key=lambda c: int(c["seq"]))
            acc, cum = 0.0, []
            for c in cs:
                acc += float(c["cost_usd"] or 0)
                cum.append(acc)
            series.append(cum)
        pts = []
        for idx in range(120):
            vals = [s[idx] for s in series if len(s) > idx]
            if len(vals) < 5:
                break
            pts.append([idx + 1, sum(vals) / len(vals), len(vals)])
        out[rid] = pts
    return out


def failure_flow(runs):
    flow = {}
    for r in runs:
        if r["id"] == "sweep-4b":
            continue
        links = {}
        for t in r["tasks"]:
            key = (t.get("stop_reason") or ("lost" if t["bucket"] in ("harness_error", "unfinished") else "none"), t["bucket"])
            links[key] = links.get(key, 0) + 1
        flow[r["id"]] = [{"stop_reason": k[0], "bucket": k[1], "n": v} for k, v in sorted(links.items())]
    return flow


def retrieval_view(ret):
    if not ret:
        return None
    t = ret["tables"]
    rule = t.get("Reading the grid", {})
    return {"source": ret["source"], "tables": t, "switch_rule_met": False if rule else None,
            "reading": "No strategy met the pre-set switch rule (a 10-point chunk-MRR gain with the 95% interval over instances "
                       "excluding 0); the default stays truncate."}


def power_block(base):
    se = base["headline"]["se"]
    return {
        "instances": base["planned"] // 3, "one_instance_moves_the_rate_pp": round(100 / (base["planned"] // 3), 1),
        "bootstrap_se_pp": round(se * 100, 1),
        "approx_minimum_detectable_gap_pp_two_independent_runs": round(2.8 * math.sqrt(2) * se * 100),
        "basis": "80% power, two-sided alpha 0.05, normal approximation with the instance-bootstrap standard error",
        "mcnemar_exact_examples": {"8:0": mcnemar_exact(8, 0), "7:1": mcnemar_exact(7, 1), "6:2": mcnemar_exact(6, 2)},
        "mcnemar_needs": mcnemar_split_table(18),
        "reading": "with 18 instances a comparison of two configurations needs a gap of about 40 points to be called clearly different",
    }


DISCLOSURES = [
    ("The 18 instances are a filtered subset of SWE-bench Verified (allowlisted repositories, a simple-fix filter, a round-robin "
     "cap, gold-validation pruning and a fixed seed). The result is not the published Verified score."),
    "These public instances were probably seen in training; the number does not predict performance on unseen issues.",
    ("Tiny sample: 18 instances, 3 runs each. The runs of one instance are strongly correlated, so 54 rows carry far less evidence "
     "than 54 independent trials."),
    "The six pytest instances pass in every run and make up a third of the instances; without them the baseline rate is lower.",
    "Every run used --no-pr: no pull request was opened, so nothing here measures PR opening.",
    "35/54 is a hand-stated substitution of sweep-4 (29/54 officially, 12 rows lost to Vertex 429) and sweep-4b (the re-run of those rows).",
    ("e1, r2 and m1 had their rows lost to 429 archived and re-run in place; the baseline used a separate re-run. Spend on those "
     "archived attempts is added back and shown both ways."),
    "Sweep-5 and r1 are partial and are shown for completeness only: never compared on a rate.",
    "Model prices are Google AI Studio list rates, not billed Vertex rates.",
]

METHODS = [
    {"id": "headline", "name": "Headline pass rate", "role": "primary", "formula": "passed / planned",
     "detail": "planned is the manifest grid (instances x runs); every planned row that is not a pass is a non-pass."},
    {"id": "boot", "name": "Bootstrap over instances", "role": "primary",
     "formula": "resample the 18 instances with replacement 20,000 times; rate = sum passes / sum planned; 2.5th and 97.5th percentiles",
     "detail": "Runs of one instance are not independent, so rows are never resampled."},
    {"id": "wilson", "name": "Wilson interval over rows", "role": "context", "formula": "Wilson score, z = 1.96",
     "detail": "Shown only to display how much clustering widens the interval."},
    {"id": "icc", "name": "Intraclass correlation and design effect", "role": "descriptive",
     "formula": "ICC(1) = (MSB - MSW) / (MSB + (m - 1) MSW); design effect = 1 + (m - 1) ICC; effective n = rows / design effect",
     "detail": "m = 3 runs per instance."},
    {"id": "cochran", "name": "Cochran's Q across run indexes", "role": "descriptive",
     "formula": "Q = (k - 1) [k sum C_j^2 - T^2] / [k T - sum R_i^2]", "detail": "Tests whether run 0, 1 and 2 differ in pass rate."},
    {"id": "passk", "name": "pass@k", "role": "descriptive", "formula": "1 - C(n - c, k) / C(n, k)", "detail": "Unbiased, per instance, averaged."},
    {"id": "mcnemar", "name": "Exact McNemar test (pre-registered)", "role": "primary",
     "formula": "two-sided binomial test of min(b, c) in b + c discordant pairs at p = 0.5",
     "detail": "On run_index 0, over the instances scored in both runs; discordant counts always shown."},
    {"id": "holm", "name": "Holm correction", "role": "primary", "formula": "p(i) adjusted = max over j<=i of (m - j + 1) p(j)",
     "detail": "Across the three variants (e1, r2, m1)."},
    {"id": "pairedboot", "name": "Paired cluster bootstrap of the difference", "role": "exploratory",
     "formula": "resample instances; difference in passes out of 3 per instance", "detail": "Context, not a verdict."},
    {"id": "sign", "name": "Exact sign test and Wilcoxon signed-rank", "role": "exploratory",
     "formula": "binomial test on instances better vs worse; Wilcoxon on per-instance pass-count differences", "detail": "Ties dropped."},
    {"id": "perm", "name": "Sign-flip permutation test", "role": "exploratory", "formula": "20,000 random sign flips of the instance differences",
     "detail": "Exchangeability of the paired differences under the null."},
    {"id": "cmh", "name": "Mantel-Haenszel odds ratio", "role": "exploratory",
     "formula": "sum(a d / n) / sum(b c / n) over instance strata; Robins-Breslow-Greenland variance",
     "detail": "Only instances where both condition and outcome vary inform it; the six always-passing pytest instances do not, so it is often undefined."},
    {"id": "h", "name": "Cohen's h", "role": "exploratory", "formula": "2 asin(sqrt p1) - 2 asin(sqrt p2)", "detail": "Effect size between two rates."},
    {"id": "mde", "name": "Minimum detectable gap", "role": "descriptive", "formula": "2.8 sqrt(2) SE", "detail": "80% power, alpha 0.05, bootstrap SE."},
    {"id": "auc", "name": "AUC for first-edit step", "role": "descriptive", "formula": "P(first edit of a passing task < first edit of a failing task)",
     "detail": "Descriptive: hard instances need more investigation before the first edit."},
    {"id": "dropped", "name": "Not computed, and why", "role": "note",
     "formula": "Rasch ranking; pooled beta-binomial posterior",
     "detail": "Eighteen items and a handful of runs cannot support a difficulty model; a pooled posterior overstates certainty at an ICC of about 0.7."},
]


def meta_block(sample):
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=BENCH, capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        sha = ""
    return {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "seed": SEED, "bootstrap_resamples": BOOT,
            "permutations": PERM, "sample": sample, "report_commit": sha, "disclosures": DISCLOSURES,
            "rules": "Generated under .claude/rules/benchmark.md: no run is pooled, intervals are over instances, comparisons use "
                     "run_index 0 and exact McNemar, partial runs are marked and never compared on a rate."}


def main():
    sample = "--sample" in sys.argv
    report = build_report(sample=sample)
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    dest = SITE_DATA / ("report.sample.json" if sample else "report.json")
    dest.write_text(json.dumps(report, indent=1, default=float) + "\n")
    print(f"wrote {dest} ({dest.stat().st_size // 1024} KiB)", file=sys.stderr)
    for r in report["runs"]:
        h = r["headline"]
        print(f"{r['id']:10s} {h['passed']:>3}/{h['planned']:<3} {100*h['rate']:5.1f}%  CI "
              f"{100*h['ci_bootstrap_instances'][0]:.1f}-{100*h['ci_bootstrap_instances'][1]:.1f}  {r['partial_mark'] or ''}", file=sys.stderr)


if __name__ == "__main__":
    main()
