"""Statistics and figures for the Phase 1 headline run: sweep-4 plus its re-run sweep-4b.

Reads only `../data/tasks.csv` and `../data/tool_use.csv` (exports of the development database) and writes
running it twice gives the same files.

    python3 -I BENCHMARK/analysis/analyze.py

Definitions (the same ones `.claude/rules/benchmark.md` states for the report):
  * a PLANNED ROW is one (instance, run_index) pair of the 18 x 3 grid;
  * the FINAL OUTCOME of a planned row is its sweep-4 row when that finished (status `completed`), else its
    sweep-4b row. sweep-4b exists only because 12 sweep-4 rows were lost to provider errors (Vertex 429s);
    a lost row says nothing about the agent, so substituting its re-run is a stated, hand-built combination,
    not something `repolace-eval report` produces (it never pools runs);
  * the headline is passes / planned rows, so every non-pass counts as a non-pass.
"""

from __future__ import annotations

import csv
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
FIG = HERE.parent / "figures"
SEED = 20261025
BOOT = 20_000
PERM = 20_000


# ----------------------------------------------------------------------------------------------- loading
def load():
    tasks = list(csv.DictReader(open(DATA / "tasks.csv", newline="")))
    tools = {r["task_id"]: r for r in csv.DictReader(open(DATA / "tool_use.csv", newline=""))}
    for t in tasks:
        t["run_index"] = int(t["run_index"])
        t["calls"] = int(t["calls"])
        t["cost"] = float(t["cost_usd"])
        t["input_tokens"] = int(t["input_tokens"])
        t["cached"] = int(t["cached_input_tokens"])
        t["output_tokens"] = int(t["output_tokens"])
        t["latency"] = float(t["latency_s"]) if t["latency_s"] else None
        t["passed"] = t["outcome"] == "passed"
        t["repo"] = t["instance_id"].split("__")[1].rsplit("-", 1)[0]
        tu = tools.get(t["task_id"])
        t["first_edit"] = int(tu["first_edit_call"]) if tu and tu["first_edit_call"] else None
        t["edit_calls"] = int(tu["edit_calls"]) if tu else 0
        t["run_calls"] = int(tu["run_calls"]) if tu else 0
        t["read_calls"] = int(tu["read_calls"]) if tu else 0
        t["submit_calls"] = int(tu["submit_calls"]) if tu else 0
    return tasks


def final_rows(tasks):
    by = {}
    for t in tasks:
        key = (t["instance_id"], t["run_index"])
        if t["run"] == "sweep-4":
            by[key] = t
    for t in tasks:
        key = (t["instance_id"], t["run_index"])
        if t["run"] == "sweep-4b" and by[key]["status"] != "completed":
            by[key] = {**t, "replaces": by[key]["task_id"]}
    return by


# ---------------------------------------------------------------------------------------------- statistics
def wilson(k, n, z=1.959964):
    if n == 0:
        return (float("nan"),) * 2
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def pct(x):
    return round(100 * x, 1)


def quantile(sorted_xs, q):
    if not sorted_xs:
        return float("nan")
    pos = (len(sorted_xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def describe(xs):
    xs = sorted(xs)
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs), "mean": round(sum(xs) / len(xs), 3), "median": round(quantile(xs, 0.5), 3),
        "p10": round(quantile(xs, 0.1), 3), "p90": round(quantile(xs, 0.9), 3), "min": xs[0], "max": xs[-1],
    }


def bootstrap_instances(per_instance, rng):
    """Percentile bootstrap over instances of the pooled pass rate (equal runs per instance)."""
    ids = list(per_instance)
    rates = []
    for _ in range(BOOT):
        sample = [rng.choice(ids) for _ in ids]
        rates.append(sum(per_instance[i][0] for i in sample) / sum(per_instance[i][1] for i in sample))
    rates.sort()
    return rates


def pass_at_k(c, n, k):
    """Unbiased estimator of P(at least one of k samples passes) from c passes in n samples."""
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def icc_oneway(groups):
    """ICC(1) by one-way ANOVA for equal group sizes of 0/1 outcomes."""
    k = len(groups)
    m = len(groups[0])
    grand = sum(sum(g) for g in groups) / (k * m)
    msb = m * sum((sum(g) / m - grand) ** 2 for g in groups) / (k - 1)
    msw = sum(sum((x - sum(g) / m) ** 2 for x in g) for g in groups) / (k * (m - 1))
    icc = (msb - msw) / (msb + (m - 1) * msw) if (msb + (m - 1) * msw) else 0.0
    return icc, msb, msw


def cochran_q(matrix):
    """Cochran's Q over blocks (rows) x treatments (columns) of 0/1; returns (Q, df, p) with df = k - 1."""
    k = len(matrix[0])
    col = [sum(r[j] for r in matrix) for j in range(k)]
    row = [sum(r) for r in matrix]
    total = sum(col)
    denom = k * total - sum(r * r for r in row)
    if denom == 0:
        return 0.0, k - 1, 1.0
    q = (k - 1) * (k * sum(c * c for c in col) - total * total) / denom
    # exact chi-square survival function for df = 2 is exp(-q/2); for other df use a series (not needed here)
    p = math.exp(-q / 2) if k - 1 == 2 else float("nan")
    return q, k - 1, p


def fisher_exact(a, b, c, d):
    """Two-sided Fisher exact test on [[a, b], [c, d]] (sum of tables no more likely than the observed)."""
    n1, n2, k = a + b, c + d, a + c
    n = n1 + n2

    def pmf(x):
        return math.comb(n1, x) * math.comb(n2, k - x) / math.comb(n, k)

    observed = pmf(a)
    lo, hi = max(0, k - n2), min(k, n1)
    p = sum(pmf(x) for x in range(lo, hi + 1) if pmf(x) <= observed * (1 + 1e-9))
    odds = (a * d) / (b * c) if b * c else float("inf")
    return min(1.0, p), odds


def perm_test(xs, ys, rng, stat=lambda a: sum(a) / len(a)):
    """Two-sided permutation test for a difference in `stat` between two samples."""
    obs = stat(xs) - stat(ys)
    pool = list(xs) + list(ys)
    n = len(xs)
    hits = 0
    for _ in range(PERM):
        rng.shuffle(pool)
        if abs(stat(pool[:n]) - stat(pool[n:])) >= abs(obs) - 1e-12:
            hits += 1
    return obs, (hits + 1) / (PERM + 1)


def auc(xs, ys):
    """P(x < y) + 0.5 P(x = y): the chance a random x is smaller than a random y."""
    wins = sum((x < y) + 0.5 * (x == y) for x in xs for y in ys)
    return wins / (len(xs) * len(ys))


# ------------------------------------------------------------------------------------------------ figures
CSS = (
    "text{font-family:system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif;fill:#1c2430}"
    ".mut{fill:#5b6675}.axis{stroke:#9aa5b4;stroke-width:1}.grid{stroke:#e4e8ee;stroke-width:1}"
)
PALETTE = {"pytest": "#2f6fdb", "sphinx": "#d98a1c", "pylint": "#7a4fc9", "seaborn": "#1a9a6a", "flask": "#c2415d"}


def svg_wrap(w, h, body, title):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
        f'aria-label="{title}"><style>{CSS}</style><rect width="{w}" height="{h}" fill="#ffffff"/>'
        f'<text x="16" y="24" font-size="15" font-weight="600">{title}</text>{body}</svg>'
    )


def fig_instances(per_instance, path):
    rows = sorted(per_instance.items(), key=lambda kv: (-kv[1][0], kv[0]))
    w, left, bar_h, gap, top = 640, 210, 16, 6, 44
    h = top + len(rows) * (bar_h + gap) + 36
    body = []
    for i in range(0, 4):
        x = left + i * 120
        body.append(f'<line class="grid" x1="{x}" y1="{top - 6}" x2="{x}" y2="{h - 28}"/>')
        body.append(f'<text class="mut" x="{x}" y="{h - 12}" font-size="11" text-anchor="middle">{i}</text>')
    for r, (inst, (passed, n)) in enumerate(rows):
        y = top + r * (bar_h + gap)
        repo = inst.split("__")[1].rsplit("-", 1)[0]
        body.append(f'<text x="{left - 8}" y="{y + 12}" font-size="12" text-anchor="end">{inst.split("__")[1]}</text>')
        body.append(f'<rect x="{left}" y="{y}" width="{360}" height="{bar_h}" fill="#f1f3f7" rx="3"/>')
        if passed:
            body.append(f'<rect x="{left}" y="{y}" width="{120 * passed}" height="{bar_h}" fill="{PALETTE[repo]}" rx="3"/>')
        body.append(f'<text class="mut" x="{left + 368}" y="{y + 12}" font-size="11">{passed}/{n}</text>')
    body.append(f'<text class="mut" x="{left + 180}" y="{h - 0}" font-size="11" text-anchor="middle">passes out of 3 runs</text>')
    path.write_text(svg_wrap(w, h + 4, "".join(body), "Passes per instance (final outcomes, sweep-4 + sweep-4b)"))


def fig_intervals(items, path):
    """items: (label, point, lo, hi, note). Horizontal dot-and-whisker on 0..100%."""
    w, left, right, top, step = 700, 270, 24, 48, 44
    h = top + len(items) * step + 30
    scale = (w - left - right) / 100.0
    body = []
    for v in range(0, 101, 20):
        x = left + v * scale
        body.append(f'<line class="grid" x1="{x:.1f}" y1="{top - 10}" x2="{x:.1f}" y2="{h - 26}"/>')
        body.append(f'<text class="mut" x="{x:.1f}" y="{h - 10}" font-size="11" text-anchor="middle">{v}%</text>')
    for i, (label, p, lo, hi, note) in enumerate(items):
        y = top + i * step + 12
        body.append(f'<text x="{left - 10}" y="{y + 4}" font-size="12" text-anchor="end">{label}</text>')
        body.append(f'<line x1="{left + lo * scale:.1f}" y1="{y}" x2="{left + hi * scale:.1f}" y2="{y}" stroke="#2f6fdb" stroke-width="3" stroke-linecap="round"/>')
        body.append(f'<circle cx="{left + p * scale:.1f}" cy="{y}" r="5.5" fill="#1c2430"/>')
        body.append(f'<text class="mut" x="{left + hi * scale + 8:.1f}" y="{y + 4}" font-size="11">{pct(p)}%</text>')
        if note:
            body.append(f'<text class="mut" x="{left - 10}" y="{y + 18}" font-size="10.5" text-anchor="end">{note}</text>')
    path.write_text(svg_wrap(w, h, "".join(body), "Pass rate with 95% intervals, by definition"))


def fig_strip(groups, path, title, xlabel, xmax):
    """groups: list of (label, values, color). One dot per task."""
    w, left, right, top, step = 700, 190, 24, 48, 56
    h = top + len(groups) * step + 34
    scale = (w - left - right) / float(xmax)
    rng = random.Random(1)
    body = []
    ticks = 5
    for i in range(ticks + 1):
        v = xmax * i / ticks
        x = left + v * scale
        body.append(f'<line class="grid" x1="{x:.1f}" y1="{top - 10}" x2="{x:.1f}" y2="{h - 28}"/>')
        body.append(f'<text class="mut" x="{x:.1f}" y="{h - 12}" font-size="11" text-anchor="middle">{v:g}</text>')
    for r, (label, vals, color) in enumerate(groups):
        y0 = top + r * step + 20
        body.append(f'<text x="{left - 10}" y="{y0 + 4}" font-size="12" text-anchor="end">{label} (n={len(vals)})</text>')
        for v in vals:
            jitter = rng.uniform(-14, 14)
            body.append(f'<circle cx="{left + v * scale:.1f}" cy="{y0 + jitter:.1f}" r="4" fill="{color}" fill-opacity="0.65"/>')
        if vals:
            m = sorted(vals)[len(vals) // 2] if len(vals) % 2 else sum(sorted(vals)[len(vals) // 2 - 1: len(vals) // 2 + 1]) / 2
            body.append(f'<line x1="{left + m * scale:.1f}" y1="{y0 - 22}" x2="{left + m * scale:.1f}" y2="{y0 + 22}" stroke="#1c2430" stroke-width="2"/>')
    body.append(f'<text class="mut" x="{left + (w - left - right) / 2}" y="{h + 2}" font-size="11" text-anchor="middle">{xlabel} (bar = median)</text>')
    path.write_text(svg_wrap(w, h + 8, "".join(body), title))


def fig_hist(values, path, title, observed_lo, observed_hi, point):
    w, h, left, top, bottom = 700, 300, 50, 48, 44
    lo, hi = 0.30, 0.95
    bins = 65
    counts = [0] * bins
    for v in values:
        i = min(bins - 1, max(0, int((v - lo) / (hi - lo) * bins)))
        counts[i] += 1
    mx = max(counts)
    bw = (w - left - 24) / bins
    body = []
    for i, c in enumerate(counts):
        bh = (h - top - bottom) * c / mx
        x = left + i * bw
        mid = lo + (i + 0.5) * (hi - lo) / bins
        color = "#2f6fdb" if observed_lo <= mid <= observed_hi else "#b9c4d6"
        body.append(f'<rect x="{x:.1f}" y="{h - bottom - bh:.1f}" width="{bw - 0.6:.1f}" height="{bh:.1f}" fill="{color}"/>')
    for v in range(30, 100, 10):
        x = left + (v / 100 - lo) / (hi - lo) * (w - left - 24)
        body.append(f'<text class="mut" x="{x:.1f}" y="{h - 24}" font-size="11" text-anchor="middle">{v}%</text>')
    px = left + (point - lo) / (hi - lo) * (w - left - 24)
    body.append(f'<line x1="{px:.1f}" y1="{top - 6}" x2="{px:.1f}" y2="{h - bottom}" stroke="#1c2430" stroke-width="2" stroke-dasharray="4 3"/>')
    body.append(f'<text class="mut" x="{left + (w - left - 24) / 2}" y="{h - 4}" font-size="11" text-anchor="middle">pooled pass rate in each resample of the 18 instances (blue = central 95%)</text>')
    path.write_text(svg_wrap(w, h, "".join(body), title))


# --------------------------------------------------------------------------------------------------- main
def main():
    rng = random.Random(SEED)
    tasks = load()
    s4 = [t for t in tasks if t["run"] == "sweep-4"]
    s4b = [t for t in tasks if t["run"] == "sweep-4b"]
    final = final_rows(tasks)
    rows = [final[k] for k in sorted(final)]
    instances = sorted({t["instance_id"] for t in rows})
    out = {"seed": SEED, "bootstrap_resamples": BOOT, "permutations": PERM}

    # ---- the grid and the headline definitions
    n_rows, n_pass = len(rows), sum(t["passed"] for t in rows)
    s4_pass = sum(t["passed"] for t in s4)
    s4_completed = [t for t in s4 if t["status"] == "completed"]
    s4_lost = [t for t in s4 if t["status"] != "completed"]
    s4b_pass = sum(t["passed"] for t in s4b)
    per_instance_final = {i: (sum(t["passed"] for t in rows if t["instance_id"] == i), 3) for i in instances}
    per_instance_s4 = {
        i: (sum(t["passed"] for t in s4 if t["instance_id"] == i), 3) for i in instances
    }
    boot_final = bootstrap_instances(per_instance_final, rng)
    boot_s4 = bootstrap_instances(per_instance_s4, rng)
    ci_final = (quantile(boot_final, 0.025), quantile(boot_final, 0.975))
    ci_s4 = (quantile(boot_s4, 0.025), quantile(boot_s4, 0.975))
    out["grid"] = {"instances": len(instances), "runs_per_instance": 3, "planned_rows": n_rows}
    out["headlines"] = {
        "sweep4_official": {
            "passed": s4_pass, "planned": 54, "rate": pct(s4_pass / 54),
            "boot_ci_instances": [pct(ci_s4[0]), pct(ci_s4[1])], "wilson_ci_rows": [pct(x) for x in wilson(s4_pass, 54)],
        },
        "sweep4_secondary_admissible": {
            "passed": s4_pass, "admissible": len(s4_completed), "rate": pct(s4_pass / len(s4_completed)),
            "wilson_ci_rows": [pct(x) for x in wilson(s4_pass, len(s4_completed))],
            "note": "excludes the 12 rows lost to provider errors; not the headline",
        },
        "sweep4b_alone": {"passed": s4b_pass, "planned": len(s4b), "rate": pct(s4b_pass / len(s4b)),
                          "wilson_ci_rows": [pct(x) for x in wilson(s4b_pass, len(s4b))]},
        "combined_final_outcomes": {
            "passed": n_pass, "planned": n_rows, "rate": pct(n_pass / n_rows),
            "boot_ci_instances": [pct(ci_final[0]), pct(ci_final[1])],
            "wilson_ci_rows_ignoring_clustering": [pct(x) for x in wilson(n_pass, n_rows)],
            "boot_se": round(math.sqrt(sum((x - sum(boot_final) / BOOT) ** 2 for x in boot_final) / (BOOT - 1)) * 100, 2),
        },
    }

    # ---- the lost rows: were they a random subset?
    lost_keys = {(t["instance_id"], t["run_index"]) for t in s4_lost}
    p_f, odds_f = fisher_exact(s4b_pass, len(s4b) - s4b_pass, s4_pass, len(s4_completed) - s4_pass)
    out["lost_rows"] = {
        "count": len(s4_lost), "reason": "Vertex AI HTTP 429 RESOURCE_EXHAUSTED after the gateway's bounded retries (provider refusal, a harness error)",
        "rerun_pass_rate": pct(s4b_pass / len(s4b)), "finished_rows_pass_rate": pct(s4_pass / len(s4_completed)),
        "fisher_p_two_sided": round(p_f, 3), "odds_ratio": round(odds_f, 2),
        "by_repo": dict(Counter(t["repo"] for t in s4_lost)),
        "wasted_spend_usd": round(sum(t["cost"] for t in s4_lost), 2),
        "bounds_if_unreplaced": {"all_fail": [s4_pass, 54, pct(s4_pass / 54)], "all_pass": [s4_pass + 12, 54, pct((s4_pass + 12) / 54)]},
    }

    # ---- per instance: consistency, pass@k
    counts = Counter(p for p, _ in per_instance_final.values())
    pk = {k: sum(pass_at_k(p, 3, k) for p, _ in per_instance_final.values()) / len(instances) for k in (1, 2, 3)}
    out["instances"] = {
        "passes_of_3_distribution": {str(k): counts.get(k, 0) for k in (0, 1, 2, 3)},
        "never_pass": sorted(i for i, (p, _) in per_instance_final.items() if p == 0),
        "always_pass": sorted(i for i, (p, _) in per_instance_final.items() if p == 3),
        "mixed": sorted(i for i, (p, _) in per_instance_final.items() if 0 < p < 3),
        "pass_at_1": pct(pk[1]), "pass_at_2": pct(pk[2]), "pass_at_3": pct(pk[3]),
        "per_instance": {i: p for i, (p, _) in per_instance_final.items()},
    }
    groups = [[int(t["passed"]) for t in sorted((r for r in rows if r["instance_id"] == i), key=lambda r: r["run_index"])] for i in instances]
    icc, msb, msw = icc_oneway(groups)
    deff = 1 + 2 * icc
    out["clustering"] = {
        "icc_oneway": round(icc, 3), "design_effect_runs_3": round(deff, 2),
        "effective_n_runs": round(n_rows / deff, 1),
        "reading": "runs of one instance are strongly correlated, so 54 rows carry far less than 54 rows of evidence",
    }

    # ---- run-index effect (paired by instance)
    matrix = [[int(next(t["passed"] for t in rows if t["instance_id"] == i and t["run_index"] == r)) for r in range(3)] for i in instances]
    q, df, p = cochran_q(matrix)
    out["run_index"] = {
        "passed_by_run_index": {str(r): sum(m[r] for m in matrix) for r in range(3)},
        "cochran_q": round(q, 3), "df": df, "p": round(p, 3),
        "reading": "no evidence the run index matters; differences are sampling noise (and run 0/1/2 were not run in order)",
    }

    # ---- by repository
    by_repo = {}
    for repo in sorted({t["repo"] for t in rows}):
        rr = [t for t in rows if t["repo"] == repo]
        k = sum(t["passed"] for t in rr)
        lo, hi = wilson(k, len(rr))
        by_repo[repo] = {"instances": len({t["instance_id"] for t in rr}), "rows": len(rr), "passed": k,
                         "rate": pct(k / len(rr)), "wilson_ci_rows": [pct(lo), pct(hi)]}
    out["by_repository"] = by_repo

    # ---- stop reason x outcome
    sr = defaultdict(lambda: Counter())
    for t in rows:
        sr[t["stop_reason"] or "(none)"][t["outcome"]] += 1
    a, b = sr["submitted"]["passed"], sr["submitted"]["failed"]
    c, d = sr["step_cap"]["passed"], sr["step_cap"]["failed"]
    p_sr, odds_sr = fisher_exact(a, b, c, d)
    out["stop_reason"] = {
        "table": {k: dict(v) for k, v in sr.items()},
        "submitted_pass_rate": pct(a / (a + b)), "step_cap_pass_rate": pct(c / (c + d)),
        "fisher_p_two_sided": float(f"{p_sr:.2g}"), "odds_ratio": round(odds_sr, 1),
        "caveat": "stop reason is an outcome of the run, not an assignment: a task that finds the fix early submits early",
        "step_cap_share": pct((c + d) / n_rows),
    }

    # ---- cost, tokens, latency (final rows; lost rows reported separately)
    cost_all = [t["cost"] for t in rows]
    cost_p = [t["cost"] for t in rows if t["passed"]]
    cost_f = [t["cost"] for t in rows if not t["passed"]]
    obs, p_cost = perm_test(cost_p, cost_f, rng)
    lat = [t["latency"] for t in rows if t["latency"]]
    lat_p = [t["latency"] for t in rows if t["passed"] and t["latency"]]
    lat_f = [t["latency"] for t in rows if not t["passed"] and t["latency"]]
    obs_l, p_lat = perm_test(lat_p, lat_f, rng)
    spent_final = sum(cost_all)
    spent_lost = sum(t["cost"] for t in s4_lost)
    out["cost"] = {
        "final_rows": describe(cost_all), "passed": describe(cost_p), "failed": describe(cost_f),
        "total_usd_final_rows": round(spent_final, 2), "total_usd_lost_rows_wasted": round(spent_lost, 2),
        "total_usd_all": round(spent_final + spent_lost, 2),
        "usd_per_pass_final_rows": round(spent_final / n_pass, 3), "usd_per_pass_all_spend": round((spent_final + spent_lost) / n_pass, 3),
        "perm_test_mean_diff_passed_minus_failed": [round(obs, 3), round(p_cost, 4)],
        "per_task_cap_usd": 2.0, "max_observed_usd": max(cost_all),
        "censoring_note": "no task reached the $2.00 cap, so cost is not censored here; latency is bounded only by the 3,600 s agent clock",
    }
    out["latency_s"] = {
        "final_rows": describe(lat), "passed": describe(lat_p), "failed": describe(lat_f),
        "perm_test_mean_diff_passed_minus_failed": [round(obs_l, 1), round(p_lat, 4)],
        "caveat": "claim to completion, with 1-3 tasks sharing the machine, the image build and index waits included",
    }
    tin = sum(t["input_tokens"] for t in rows)
    out["tokens"] = {
        "calls_per_task": describe([t["calls"] for t in rows]),
        "input_tokens_total": tin, "cached_share": pct(sum(t["cached"] for t in rows) / tin),
        "output_tokens_total": sum(t["output_tokens"] for t in rows),
        "input_tokens_per_task_mean": round(tin / n_rows), "output_tokens_per_task_mean": round(sum(t["output_tokens"] for t in rows) / n_rows),
    }

    # ---- how the agent spent its steps: the finding that motivated the early-edit nudge
    with_edit = [t for t in rows if t["first_edit"] is not None and t["status"] == "completed"]
    fe_pass = [t["first_edit"] for t in with_edit if t["passed"]]
    fe_fail = [t["first_edit"] for t in with_edit if not t["passed"]]
    obs_e, p_e = perm_test(fe_pass, fe_fail, rng, stat=lambda a: sorted(a)[len(a) // 2])
    obs_em, p_em = perm_test(fe_pass, fe_fail, rng)
    cap_fail = [t for t in rows if t["stop_reason"] == "step_cap" and not t["passed"]]
    cap_pass = [t for t in rows if t["stop_reason"] == "step_cap" and t["passed"]]
    sub_pass = [t for t in rows if t["stop_reason"] == "submitted" and t["passed"]]
    completed = [t for t in rows if t["status"] == "completed"]
    out["step_use"] = {
        "tasks_analysed": len(with_edit), "tasks_never_editing": sum(1 for t in completed if t["first_edit"] is None),
        "first_edit_call_passed": describe(fe_pass), "first_edit_call_failed": describe(fe_fail),
        "perm_test_median_diff": [obs_e, round(p_e, 4)], "perm_test_mean_diff": [round(obs_em, 2), round(p_em, 4)],
        "auc_passed_edits_earlier_than_failed": round(auc(fe_pass, fe_fail), 3),
        "step_cap_failed_first_edit": describe([t["first_edit"] for t in cap_fail if t["first_edit"]]),
        "step_cap_passed_first_edit": describe([t["first_edit"] for t in cap_pass if t["first_edit"]]),
        "submitted_passed_first_edit": describe([t["first_edit"] for t in sub_pass if t["first_edit"]]),
        "tool_calls_share_in_step_cap_failures": {
            "run_tests_or_run_python": pct(sum(t["run_calls"] for t in cap_fail) / sum(t["run_calls"] + t["read_calls"] + t["edit_calls"] + t["submit_calls"] for t in cap_fail)),
            "read_search": pct(sum(t["read_calls"] for t in cap_fail) / sum(t["run_calls"] + t["read_calls"] + t["edit_calls"] + t["submit_calls"] for t in cap_fail)),
            "edit": pct(sum(t["edit_calls"] for t in cap_fail) / sum(t["run_calls"] + t["read_calls"] + t["edit_calls"] + t["submit_calls"] for t in cap_fail)),
        },
        "note": "descriptive: the first edit comes later in failures partly because hard instances need more investigation",
    }

    # ---- power: what difference could this design detect?
    se = out["headlines"]["combined_final_outcomes"]["boot_se"] / 100
    out["power"] = {
        "instances": len(instances), "one_instance_moves_the_rate_by_pp": round(100 / len(instances), 1),
        "boot_se_pp": round(se * 100, 1), "approx_minimum_detectable_gap_pp_two_independent_models": round(2.8 * math.sqrt(2) * se * 100),
        "mcnemar_exact_examples": {"8:0": round(2 * 0.5 ** 8, 3), "7:1": round(2 * (1 + 8) * 0.5 ** 8, 3), "6:2": round(min(1.0, 2 * (1 + 8 + 28) * 0.5 ** 8), 3)},
        "reading": "a comparison of two configurations on these 18 instances needs a gap of roughly 40 points (two independent configurations) to be called clearly different",
    }

    # ---- sensitivity of the headline to how rows are counted
    out["sensitivity"] = [
        {"definition": "sweep-4 official headline (passed / planned, lost rows count as non-passes)", "k": s4_pass, "n": 54},
        {"definition": "sweep-4 secondary (passed / admissible; excludes the 12 lost rows)", "k": s4_pass, "n": len(s4_completed)},
        {"definition": "combined final outcomes (lost rows replaced by their sweep-4b re-run)", "k": n_pass, "n": n_rows},
        {"definition": "conservative: count only passes where the agent called submit", "k": sum(1 for t in rows if t["passed"] and t["stop_reason"] == "submitted"), "n": n_rows},
        {"definition": "pytest instances removed (the 6 always-pass instances)", "k": n_pass - 18, "n": n_rows - 18},
    ]
    for row in out["sensitivity"]:
        row["rate"] = pct(row["k"] / row["n"])
        row["wilson_ci_rows"] = [pct(x) for x in wilson(row["k"], row["n"])]

    # ---- figures
    FIG.mkdir(exist_ok=True)
    fig_instances(per_instance_final, FIG / "instance_passes.svg")
    fig_intervals(
        [
            ("sweep-4 official (29/54)", s4_pass / 54, ci_s4[0], ci_s4[1], "lost rows count as failures"),
            ("sweep-4 admissible (29/42)", s4_pass / len(s4_completed), *wilson(s4_pass, len(s4_completed)), "row-level Wilson, ignores clustering"),
            ("combined final (35/54)", n_pass / n_rows, ci_final[0], ci_final[1], "bootstrap over 18 instances"),
            ("combined, submit-only passes", out["sensitivity"][3]["k"] / n_rows, *wilson(out["sensitivity"][3]["k"], n_rows), "row-level Wilson"),
        ],
        FIG / "pass_rate_intervals.svg",
    )
    fig_hist(boot_final, FIG / "bootstrap_distribution.svg", "Bootstrap over instances: how much the pass rate moves", ci_final[0], ci_final[1], n_pass / n_rows)
    fig_strip(
        [("passed", fe_pass, "#1a9a6a"), ("failed", fe_fail, "#c2415d")],
        FIG / "first_edit_by_outcome.svg", "Model call of the first edit, by outcome (finished tasks)", "call number of first edit_file/create_file", 40,
    )
    fig_strip(
        [("passed", cost_p, "#1a9a6a"), ("failed", cost_f, "#c2415d")],
        FIG / "cost_by_outcome.svg", "Cost per task in USD, by outcome", "USD", 2,
    )

    (HERE / "results.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: out[k] for k in ("headlines", "lost_rows", "instances", "clustering", "run_index", "stop_reason", "cost", "step_use", "power")}, indent=1)[:12000])


if __name__ == "__main__":
    main()
