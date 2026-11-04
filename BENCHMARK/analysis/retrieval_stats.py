#!/usr/bin/env python3
"""Statistics for the retrieval grid, from the JSON `harness.retrieval_eval.to_json(EvalRun)` writes. Writes retrieval.json.

    uv run --all-packages python BENCHMARK/analysis/retrieval_stats.py INPUT.json [--out PATH] [--tasks tasks.csv]
    uv run --all-packages python BENCHMARK/analysis/retrieval_stats.py --placeholder [--out PATH]

`INPUT.json` is `eval/runs/<run>/report.json`. The default `--out` is the public site's data file,
`api/repolace_api/site/retrieval_site/data/retrieval.json`. NEVER point `INPUT` at the synthetic fixture with the default `--out`: the
fixture is for tests and a dev preview into a temp directory (the output carries `synthetic: true` and the page shows a banner).
`--placeholder` writes the "awaiting retrieval-2" state from the retrieval-1 aggregates in BENCHMARK/data/retrieval.json: point
estimates and the intervals that run printed, no per-instance panels.

What is computed (the unit is always the INSTANCE; chunks and queries inside an instance are never resampled):
* per cell (4 index x 2 query strategies): chunk/file MRR and recall@5/10/20 with 95% percentile bootstrap intervals, and how many
  instances each metric scored (an instance with no gold of a kind is skipped by that kind, so denominators differ);
* paired differences of the 7 other cells against truncate/truncate on the instances both scored: mean, interval, paired
  standardised effect size d_z, a sign-flip permutation p, and Holm-adjusted p over the 7 comparisons (exploratory);
* the pre-registered switch rule: chunk-MRR gain >= 10 pp AND the 95% interval excludes 0;
* minimum detectable effect and a power curve, from the observed SD of the paired differences (normal approximation);
* reciprocal-rank distributions, per-instance heatmap, per-repository breakdown (description only), unreachable hunks;
* an exploratory relation between baseline retrieval and sweep-4 pass/fail (Spearman, AUC, both with instance bootstrap).
"""

import argparse
import csv
import json
import math
import random
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import stats as S  # noqa: E402  (reuses holm, quantile, auc_less; the analysis directory is not a package)

REPO_ROOT = HERE.parents[1]
SITE_DATA = REPO_ROOT / "api" / "repolace_api" / "site" / "retrieval_site" / "data"
RETRIEVAL_1 = REPO_ROOT / "BENCHMARK" / "data" / "retrieval.json"
DEFAULT_TASKS = REPO_ROOT / "BENCHMARK" / "data" / "tasks.csv"

SEED = 20261104
BOOT = 10_000
PERM = 20_000
BASELINE = ("truncate", "truncate")
MIN_GAIN_PP = 10.0
PRIMARY = "chunk_mrr"
KS = (5, 10, 20)
ALPHA = 0.05
POWER = 0.8
SWEEP = "sweep-4"

METRICS = [("chunk_mrr", "chunk MRR"), ("file_mrr", "file MRR")] + [
    (f"{kind}_r{k}", f"{kind} R@{k}") for kind in ("chunk", "file") for k in KS
]
METRIC_LABEL = dict(METRICS)

#: .claude/rules/open-questions.md, "Embedding truncation". Measured over this repository's own 238 chunks, not over the benchmark's repositories.
TRUNCATION = {
    "max_seq_length_tokens": 128,
    "chunks_measured": 238,
    "share_over_budget": 0.416,
    "median_chunk_tokens": 105,
    "share_over_budget_by_type": {"function": 0.527, "module": 0.469, "class_skeleton": 0.333, "method": 0.304},
    "longest_chunk_tokens": 1328,
    "source": ".claude/rules/open-questions.md, embedding truncation (measured over this repository, not the benchmark repositories)",
    "model": "flax-sentence-embeddings/st-codesearch-distilroberta-base",
}


# ------------------------------------------------------------------------------------------------ helpers
def cell_key(strategy: str, query: str) -> str:
    return f"{strategy} / {query}"


BASE_KEY = cell_key(*BASELINE)


def rng_for(label: str) -> random.Random:
    return random.Random(f"{SEED}/{label}")


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def sd(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def percentile_interval(stats_sorted, confidence=0.95):
    tail = (1 - confidence) / 2
    return [S.quantile(stats_sorted, tail), S.quantile(stats_sorted, 1 - tail)]


def bootstrap_mean(values, label, resamples=None):
    """95% percentile interval for the mean, resampling whole instances. `None` below two values."""
    n = len(values)
    if n < 2:
        return None
    rng = rng_for(label)
    means = sorted(sum(values[i] for i in rng.choices(range(n), k=n)) / n for _ in range(resamples or BOOT))
    return percentile_interval(means)


def sign_flip_p(diffs, label, perms=None):
    """Two-sided sign-flip permutation test of mean(diffs) = 0 (paired differences are symmetric about 0 under the null)."""
    nz = [d for d in diffs if d != 0]
    if not nz:
        return 1.0
    rng = rng_for(label)
    obs = abs(sum(nz))
    n = len(nz)
    perms = perms or PERM
    hits = 0
    for _ in range(perms):
        bits = rng.getrandbits(n)
        s = sum(d if (bits >> i) & 1 else -d for i, d in enumerate(nz))
        if abs(s) >= obs - 1e-12:
            hits += 1
    return (hits + 1) / (perms + 1)


def power_at(delta_pp, sd_pp, n, alpha=ALPHA):
    """Power of a two-sided paired test to see a true mean difference `delta_pp`, normal approximation."""
    if not sd_pp:
        return None
    z = NormalDist().inv_cdf(1 - alpha / 2)
    shift = abs(delta_pp) * math.sqrt(n) / sd_pp
    nd = NormalDist()
    return nd.cdf(shift - z) + nd.cdf(-shift - z)


def mde_pp(sd_pp, n, alpha=ALPHA, power=POWER):
    if not sd_pp:
        return None
    nd = NormalDist()
    return (nd.inv_cdf(1 - alpha / 2) + nd.inv_cdf(power)) * sd_pp / math.sqrt(n)


def rank_of(rr):
    return None if rr is None else (None if rr <= 0 else round(1 / rr))


RANK_BINS = [("1", 1, 1), ("2", 2, 2), ("3-5", 3, 5), ("6-10", 6, 10), ("11-20", 11, 20)]


def rank_bins(rrs):
    counts = {name: 0 for name, _, _ in RANK_BINS}
    counts["miss"] = 0
    for rr in rrs:
        r = rank_of(rr)
        if r is None:
            counts["miss"] += 1
            continue
        for name, lo, hi in RANK_BINS:
            if lo <= r <= hi:
                counts[name] += 1
                break
        else:
            counts["miss"] += 1
    return counts


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return out

    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True)) / math.sqrt(sxx * syy)


# ------------------------------------------------------------------------------------------------ loading
def metric_values(m: dict) -> dict:
    """Flatten one InstanceResult's `metrics` into {metric key: value or None}. JSON turns the int keys of the recall dicts into strings."""
    out = {"chunk_mrr": m.get("chunk_rr"), "file_mrr": m.get("file_rr")}
    for kind in ("chunk", "file"):
        recall = m.get(f"{kind}_recall") or {}
        for k in KS:
            out[f"{kind}_r{k}"] = recall.get(str(k), recall.get(k))
    return out


def load_run(raw: dict):
    """-> (cells in grid order, {cell: {instance: values}}, {instance: repo}, {(cell, instance): unreachable})."""
    cells = [(s, q) for s in raw["strategies"] for q in raw["query_strategies"]]
    by_cell: dict = {c: {} for c in cells}
    repo_of: dict = {}
    unreachable: dict = {}
    for r in raw["results"]:
        cell = (r["strategy"], r["query_strategy"])
        by_cell.setdefault(cell, {})[r["instance_id"]] = metric_values(r["metrics"])
        repo_of[r["instance_id"]] = r["repo"]
        unreachable[(cell, r["instance_id"])] = r["metrics"].get("unreachable_hunks", 0)
    return cells, by_cell, repo_of, unreachable


# ------------------------------------------------------------------------------------------------ blocks
def cell_block(cell, by_cell, n_instances):
    key = cell_key(*cell)
    metrics = {}
    for mkey, label in METRICS:
        pairs = sorted((i, v[mkey]) for i, v in by_cell[cell].items() if v[mkey] is not None)
        vals = [v for _, v in pairs]
        interval = bootstrap_mean(vals, f"cell/{key}/{mkey}")
        metrics[mkey] = {
            "label": label, "mean": mean(vals), "ci": interval, "scored": len(vals), "skipped": n_instances - len(vals),
            "sd": sd(vals),
        }
    return {"key": key, "strategy": cell[0], "query_strategy": cell[1], "is_baseline": cell == BASELINE, "metrics": metrics}


def paired_block(cell, by_cell, metric_key):
    key = cell_key(*cell)
    base, other = by_cell[BASELINE], by_cell[cell]
    ids = sorted(i for i in other if i in base and other[i][metric_key] is not None and base[i][metric_key] is not None)
    diffs = [other[i][metric_key] - base[i][metric_key] for i in ids]
    label = f"paired/{key}/{metric_key}"
    m = mean(diffs)
    s = sd(diffs)
    interval = bootstrap_mean(diffs, label)
    return {
        "cell": key, "strategy": cell[0], "query_strategy": cell[1], "metric": metric_key, "paired": len(diffs),
        "mean_pp": None if m is None else m * 100, "ci_pp": None if interval is None else [x * 100 for x in interval],
        "sd_pp": None if s is None else s * 100,
        "dz": None if not s else m / s,
        "p": sign_flip_p(diffs, label) if diffs else None,
        "wins": sum(1 for d in diffs if d > 1e-12), "losses": sum(1 for d in diffs if d < -1e-12),
        "ties": sum(1 for d in diffs if abs(d) <= 1e-12),
        "diffs_pp": {i: d * 100 for i, d in zip(ids, diffs, strict=True)},
    }


def comparisons_block(cells, by_cell):
    others = [c for c in cells if c != BASELINE]
    out = {}
    for mkey, label in METRICS:
        rows = [paired_block(c, by_cell, mkey) for c in others]
        ps = [r["p"] if r["p"] is not None else 1.0 for r in rows]
        adj = S.holm(ps) if rows else []
        for r, a in zip(rows, adj, strict=True):
            r["p_holm"] = a
            r["meets_switch_rule"] = bool(
                mkey == PRIMARY and r["mean_pp"] is not None and r["ci_pp"] is not None
                and r["mean_pp"] >= MIN_GAIN_PP and r["ci_pp"][0] > 0
            )
        out[mkey] = {"label": label, "rows": rows}
    return out


def switch_rule_block(comparisons):
    rows = comparisons[PRIMARY]["rows"]
    met = [r["cell"] for r in rows if r["meets_switch_rule"]]
    best = max((r for r in rows if r["mean_pp"] is not None), key=lambda r: r["mean_pp"], default=None)
    return {
        "metric": PRIMARY, "min_gain_pp": MIN_GAIN_PP, "rule": "mean paired gain >= min_gain_pp AND the 95% interval excludes 0",
        "comparisons": len(rows), "met": met, "any_met": bool(met), "default_stays": BASE_KEY if not met else None,
        "largest_gain_cell": best["cell"] if best else None, "largest_gain_pp": best["mean_pp"] if best else None,
        "largest_gain_ci_pp": best["ci_pp"] if best else None,
        "cells_with_interval_excluding_zero_but_below_threshold": [
            r["cell"] for r in rows if r["ci_pp"] and r["ci_pp"][0] > 0 and r["cell"] not in met
        ],
    }


def power_block(comparisons, n):
    rows = comparisons[PRIMARY]["rows"]
    sds = [r["sd_pp"] for r in rows if r["sd_pp"]]
    median_sd = S.quantile(sorted(sds), 0.5) if sds else None
    holm_alpha = ALPHA / max(len(rows), 1)
    grid = [i * 0.5 for i in range(61)]  # 0 to 30 pp
    ns = list(range(6, 101, 2))
    return {
        "n": n, "alpha": ALPHA, "target_power": POWER, "holm_alpha": holm_alpha, "median_sd_pp": median_sd,
        "mde_pp_median_sd": mde_pp(median_sd, n), "mde_pp_median_sd_holm": mde_pp(median_sd, n, holm_alpha),
        "power_at_min_gain_median_sd": power_at(MIN_GAIN_PP, median_sd, n) if median_sd else None,
        "per_comparison": [
            {"cell": r["cell"], "sd_pp": r["sd_pp"], "mde_pp": mde_pp(r["sd_pp"], n), "mde_pp_holm": mde_pp(r["sd_pp"], n, holm_alpha),
             "power_at_min_gain": power_at(MIN_GAIN_PP, r["sd_pp"], n),
             "observed_pp": r["mean_pp"]}
            for r in rows
        ],
        "curves": [{"cell": r["cell"], "sd_pp": r["sd_pp"], "points": [[d, power_at(d, r["sd_pp"], n)] for d in grid]}
                   for r in rows if r["sd_pp"]],
        "median_curve": {"sd_pp": median_sd, "points": [[d, power_at(d, median_sd, n)] for d in grid]} if median_sd else None,
        "n_curve": {"delta_pp": MIN_GAIN_PP, "sd_pp": median_sd, "points": [[k, power_at(MIN_GAIN_PP, median_sd, k)] for k in ns]} if median_sd else None,
        "basis": "two-sided paired test, normal approximation, 80% power at alpha 0.05, using the observed SD of the per-instance "
                 "differences. The SD is itself estimated from 18 instances, and a t-based calculation would need slightly more.",
    }


def rr_block(cells, by_cell):
    out = []
    for c in cells:
        entry = {"key": cell_key(*c)}
        for mkey in ("chunk_mrr", "file_mrr"):
            pairs = sorted((i, v[mkey]) for i, v in by_cell[c].items() if v[mkey] is not None)
            vals = [v for _, v in pairs]
            entry[mkey] = {
                "values": [{"instance": i, "rr": v} for i, v in pairs], "bins": rank_bins(vals),
                "share_miss": sum(1 for v in vals if v == 0) / len(vals) if vals else None,
                "share_rank1": sum(1 for v in vals if v == 1) / len(vals) if vals else None,
                "median": S.quantile(sorted(vals), 0.5) if vals else None,
            }
        out.append(entry)
    return {"rank_bins": [b[0] for b in RANK_BINS] + ["miss"], "cells": out}


def heatmap_block(cells, by_cell, repo_of):
    ids = sorted(repo_of)
    rows = []
    for i in ids:
        rrs = {cell_key(*c): by_cell[c].get(i, {}).get("chunk_mrr") for c in cells}
        scored = [v for v in rrs.values() if v is not None]
        rows.append({"instance": i, "repo": repo_of[i], "chunk_rr": rrs,
                     "file_rr": {cell_key(*c): by_cell[c].get(i, {}).get("file_mrr") for c in cells},
                     "mean_chunk_rr": mean(scored)})
    rows.sort(key=lambda r: (-(r["mean_chunk_rr"] if r["mean_chunk_rr"] is not None else -1), r["instance"]))
    return {"cells": [cell_key(*c) for c in cells], "rows": rows}


def repo_block(cells, by_cell, repo_of):
    repos = sorted(set(repo_of.values()))
    n = len(repo_of)
    out = []
    for repo in repos:
        ids = sorted(i for i, r in repo_of.items() if r == repo)
        per_cell = []
        for c in cells:
            vals = [by_cell[c][i]["chunk_mrr"] for i in ids if i in by_cell[c] and by_cell[c][i]["chunk_mrr"] is not None]
            fvals = [by_cell[c][i]["file_mrr"] for i in ids if i in by_cell[c] and by_cell[c][i]["file_mrr"] is not None]
            base = [by_cell[BASELINE][i]["chunk_mrr"] for i in ids if i in by_cell[c] and by_cell[c][i]["chunk_mrr"] is not None
                    and by_cell[BASELINE].get(i, {}).get("chunk_mrr") is not None]
            paired = [by_cell[c][i]["chunk_mrr"] - by_cell[BASELINE][i]["chunk_mrr"] for i in ids
                      if by_cell[c].get(i, {}).get("chunk_mrr") is not None and by_cell[BASELINE].get(i, {}).get("chunk_mrr") is not None]
            per_cell.append({"cell": cell_key(*c), "chunk_mrr": mean(vals), "file_mrr": mean(fvals), "scored": len(vals),
                             "delta_pp": None if not paired or c == BASELINE else mean(paired) * 100, "baseline_scored": len(base)})
        out.append({"repo": repo, "instances": len(ids), "share": len(ids) / n, "instance_ids": ids, "cells": per_cell})
    top2 = sorted(out, key=lambda r: -r["instances"])[:2]
    return {
        "repos": out, "description_only": True,
        "top_two": [r["repo"] for r in top2], "top_two_instances": sum(r["instances"] for r in top2), "total_instances": n,
        "note": "One to six instances per repository: these are descriptions, with no intervals and no tests.",
    }


def unreachable_block(cells, unreachable, repo_of):
    ids = sorted(repo_of)
    per = {}
    consistent = True
    for i in ids:
        vals = {unreachable.get((c, i)) for c in cells if (c, i) in unreachable}
        if len(vals) > 1:
            consistent = False
        per[i] = max((v for v in vals if v is not None), default=0)
    return {
        "total_hunks": sum(per.values()), "instances_with_any": sum(1 for v in per.values() if v > 0), "instances": len(ids),
        "per_instance": [{"instance": i, "unreachable_hunks": per[i]} for i in ids if per[i] > 0],
        "same_in_every_cell": consistent,
        "meaning": "Gold hunks no indexed chunk overlaps. They are dropped from the chunk metrics, not scored as misses.",
    }


def denominators_block(cells, by_cell, n):
    return {"instances": n, "cells": [{"key": cell_key(*c), "file_scored": sum(1 for v in by_cell[c].values() if v["file_mrr"] is not None),
                                       "chunk_scored": sum(1 for v in by_cell[c].values() if v["chunk_mrr"] is not None)} for c in cells]}


def sweep_block(by_cell, repo_of, tasks_csv: Path | None):
    """Exploratory: retrieval success (truncate / truncate chunk RR) against the sweep-4 outcome of the same instance."""
    base = {i: v["chunk_mrr"] for i, v in by_cell[BASELINE].items() if v["chunk_mrr"] is not None}
    out = {"run": SWEEP, "exploratory": True, "retrieval_cell": BASE_KEY, "metric": "chunk reciprocal rank"}
    if tasks_csv is None or not Path(tasks_csv).exists():
        return {**out, "available": False, "reason": "tasks.csv not found"}
    with open(tasks_csv, newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["run"] == SWEEP]
    valid = [r for r in rows if r["outcome"] in ("passed", "failed")]
    lost = [r for r in rows if r["outcome"] not in ("passed", "failed")]
    per = []
    for iid in sorted(base):
        mine = [r for r in valid if r["instance_id"] == iid]
        if not mine:
            continue
        p = sum(1 for r in mine if r["outcome"] == "passed")
        per.append({"instance": iid, "repo": repo_of.get(iid), "rr": base[iid], "runs": len(mine), "passes": p, "pass_rate": p / len(mine),
                    "lost_rows": sum(1 for r in lost if r["instance_id"] == iid)})
    out.update({
        "available": True, "rows_total": len(rows), "rows_used": len(valid), "rows_excluded_harness_loss": len(lost),
        "exclusion": "rows with an empty outcome (a harness loss, mostly provider 429s) are excluded, not counted as failures",
        "instances": len(per), "per_instance": per,
    })
    if len(per) < 4:
        return {**out, "spearman": None, "auc": None}
    rr = [p["rr"] for p in per]
    pr = [p["pass_rate"] for p in per]
    rho = spearman(rr, pr)
    rng = rng_for("sweep/spearman")
    boots, perm_hits = [], 0
    for _ in range(BOOT):
        idx = rng.choices(range(len(per)), k=len(per))
        r = spearman([rr[i] for i in idx], [pr[i] for i in idx])
        if r is not None:
            boots.append(r)
    boots.sort()
    prng = rng_for("sweep/spearman-perm")
    shuffled = pr[:]
    for _ in range(PERM):
        prng.shuffle(shuffled)
        r = spearman(rr, shuffled)
        if rho is not None and r is not None and abs(r) >= abs(rho) - 1e-12:
            perm_hits += 1
    ever = [p["rr"] for p in per if p["passes"] > 0]
    never = [p["rr"] for p in per if p["passes"] == 0]
    auc = S.auc_less(never, ever)  # P(never-solved rr < ever-solved rr) + half ties: 0.5 is no relation

    def auc_stat(idx):
        a = [rr[i] for i in idx if per[i]["passes"] > 0]
        b = [rr[i] for i in idx if per[i]["passes"] == 0]
        return S.auc_less(b, a)

    arng = rng_for("sweep/auc")
    aboots = sorted(v for v in (auc_stat(arng.choices(range(len(per)), k=len(per))) for _ in range(BOOT)) if v is not None)
    hit = [p for p in per if p["rr"] > 0]
    miss = [p for p in per if p["rr"] == 0]
    out.update({
        "spearman": {"rho": rho, "ci": percentile_interval(boots) if boots else None, "p_permutation": (perm_hits + 1) / (PERM + 1),
                     "n": len(per)},
        "auc": {"value": auc, "ci": percentile_interval(aboots) if aboots else None, "n_ever_passed": len(ever), "n_never_passed": len(never),
                "reading": "probability that a random ever-solved instance had a better retrieval rank than a random never-solved one; 0.5 is no relation"},
        "hit_split": {"hit": {"instances": len(hit), "mean_pass_rate": mean([p["pass_rate"] for p in hit])},
                      "miss": {"instances": len(miss), "mean_pass_rate": mean([p["pass_rate"] for p in miss])}},
        "power_note": f"{len(per)} instances: an interval of this width cannot separate a modest relation from none.",
    })
    return out


# ------------------------------------------------------------------------------------------------ assembly
def design_block(cells):
    return {
        "strategies": sorted({c[0] for c in cells}, key=lambda s: ["truncate", "head_tail", "windows", "signature_docstring"].index(s)
                             if s in ["truncate", "head_tail", "windows", "signature_docstring"] else 9),
        "query_strategies": sorted({c[1] for c in cells}), "baseline": BASE_KEY, "cells": [cell_key(*c) for c in cells],
        "comparisons": len(cells) - 1, "primary_metric": PRIMARY, "min_gain_pp": MIN_GAIN_PP,
        "preregistered": "The switch rule was written down before any number existed: a cell replaces truncate / truncate only if "
                         "its paired chunk-MRR gain is at least 10 percentage points and the 95% interval over instances excludes zero.",
    }


def build(raw: dict, tasks_csv: Path | None = DEFAULT_TASKS, source: str = "") -> dict:
    cells, by_cell, repo_of, unreachable = load_run(raw)
    if BASELINE not in by_cell or not by_cell[BASELINE]:
        raise SystemExit(f"the run has no {BASE_KEY} cell, so there is nothing to pair against")
    n = len(repo_of)
    comparisons = comparisons_block(cells, by_cell)
    return {
        "status": "ready",
        "synthetic": "_synthetic" in raw,
        "meta": {
            "source": source, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "seed": SEED,
            "bootstrap_resamples": BOOT, "permutations": PERM, "instances": n,
            "interval": "95% percentile bootstrap over instances", "ks": list(KS),
            "skipped_instances": raw.get("skipped_instances", []), "dropped_targets": raw.get("dropped_targets", []),
            "plans": raw.get("plans", []), "chunks_written": sum(r.get("chunks_written", 0) for r in raw["results"]
                                                                  if (r["strategy"], r["query_strategy"]) == BASELINE),
        },
        "design": design_block(cells),
        "cells": [cell_block(c, by_cell, n) for c in cells],
        "denominators": denominators_block(cells, by_cell, n),
        "comparisons": comparisons,
        "switch_rule": switch_rule_block(comparisons),
        "power": power_block(comparisons, n),
        "distributions": rr_block(cells, by_cell),
        "heatmap": heatmap_block(cells, by_cell, repo_of),
        "repos": repo_block(cells, by_cell, repo_of),
        "unreachable": unreachable_block(cells, unreachable, repo_of),
        "sweep_link": sweep_block(by_cell, repo_of, tasks_csv),
        "truncation": TRUNCATION,
    }


# ------------------------------------------------------------------------------------------------ placeholder
def _pct(s):
    s = s.strip()
    return None if s in ("n/a", "") else round(float(s.rstrip("%")) / 100, 4)


def _delta(s):
    m = re.match(r"([+-][\d.]+) pp \[([+-][\d.]+), ([+-][\d.]+)\]", s)
    return [float(m.group(1)), float(m.group(2)), float(m.group(3))] if m else None


def placeholder(path: Path = RETRIEVAL_1) -> dict:
    """The awaiting state: retrieval-1 aggregates only, nothing per instance, nothing recomputed."""
    raw = json.loads(Path(path).read_text())
    tables = raw["tables"]
    pooled = tables["By embedding strategy (all repositories pooled)"]["rows"]
    cells = []
    for row in pooled:
        label = row[0]
        strategy, query = (x.strip() for x in label.split("/"))
        scored = re.match(r"(\d+) / (\d+) of (\d+)", row[1])
        names = ["file_r5", "file_r10", "file_r20", "chunk_r5", "chunk_r10", "chunk_r20", "file_mrr", "chunk_mrr"]
        cells.append({"key": cell_key(strategy, query), "strategy": strategy, "query_strategy": query,
                      "file_scored": int(scored.group(1)), "chunk_scored": int(scored.group(2)), "instances": int(scored.group(3)),
                      **{n_: _pct(v) for n_, v in zip(names, row[2:], strict=True)}})
    deltas = []
    for row in tables["Reading the grid"]["rows"]:
        c = _delta(row[2])
        f = _delta(row[4])
        deltas.append({"cell": row[0], "paired": int(row[1]), "chunk_mrr_pp": c, "file_mrr_pp": f, "meets_switch_rule": row[3] == "yes"})
    plan = [{"repo": r[0], "instances": int(r[1]), "first_index_chunks": int(r[2]), "status": r[3]}
            for r in tables["Plan"]["rows"]]
    n = sum(p["instances"] for p in plan)
    implied = []
    for d in deltas:
        if d["chunk_mrr_pp"]:
            _m, lo, hi = d["chunk_mrr_pp"]
            implied.append({"cell": d["cell"], "sd_pp": (hi - lo) / (2 * 1.96) * math.sqrt(n)})
    sds = sorted(i["sd_pp"] for i in implied)
    med = S.quantile(sds, 0.5) if sds else None
    return {
        "status": "awaiting",
        "synthetic": False,
        "meta": {"source": raw.get("source", "retrieval-1"), "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                 "seed": SEED, "bootstrap_resamples": BOOT, "permutations": PERM, "instances": n,
                 "interval": "95% percentile bootstrap over instances", "ks": list(KS)},
        "design": {"baseline": BASE_KEY, "cells": [c["key"] for c in cells], "comparisons": len(cells) - 1, "primary_metric": PRIMARY,
                   "min_gain_pp": MIN_GAIN_PP,
                   "preregistered": "The switch rule was written down before any number existed: a cell replaces truncate / truncate only if "
                                    "its paired chunk-MRR gain is at least 10 percentage points and the 95% interval over instances excludes zero."},
        "awaiting": {"run": "retrieval-2", "reason": "The full per-instance run (about 12.7 hours on CPU) is still in progress. The per-instance "
                                                   "panels, the Holm-adjusted tests, the power analysis and the pass/fail relation need it."},
        "retrieval1": {"run": "retrieval-1", "plan": plan, "instances": n, "cells": cells, "deltas": deltas,
                       "implied_sd_pp": implied, "median_implied_sd_pp": med,
                       "mde_pp_median": mde_pp(med, n) if med else None,
                       "implied_note": "Standard deviation of the paired differences, recovered from the printed interval half-width "
                                       "(half-width / 1.96 x sqrt(n)); intervals are asymmetric, so this is approximate."},
        "truncation": TRUNCATION,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("input", nargs="?", help="report.json from `repolace-eval retrieval`")
    ap.add_argument("--out", type=Path, default=SITE_DATA / "retrieval.json")
    ap.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    ap.add_argument("--placeholder", action="store_true")
    args = ap.parse_args(argv)
    if args.placeholder:
        data = placeholder()
    elif args.input:
        data = build(json.loads(Path(args.input).read_text()), args.tasks, source=str(args.input))
    else:
        ap.error("give an INPUT.json or --placeholder")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {args.out} (status {data['status']}{', SYNTHETIC' if data.get('synthetic') else ''})")


if __name__ == "__main__":
    main()
