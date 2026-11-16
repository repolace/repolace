#!/usr/bin/env python3
"""sweep-6, a second run of the sweep-4 configuration: its figures, and the hand-built combinations with sweep-4.

Reads BENCHMARK/data/{sweep-4,sweep-4b,sweep-6,sweep-6b}/ (exports `export_runs.py` wrote) and writes
BENCHMARK/data/replication.json. It does not touch site/data/report.json, and the /benchmark/ page does not draw
sweep-6: the headline stays sweep-4's official 29 of 54 (`.claude/rules/api-and-ui.md`). docs/benchmark.md, which the
docs site publishes, quotes these figures labelled.

    uv run --all-packages python BENCHMARK/analysis/replication_stats.py

Every figure here except `sweep-6 official` is built by hand, not by `repolace-eval report`, which never pools or
substitutes runs:
  * a SUBSTITUTED run replaces each row that did not finish (a harness error) by its re-run, through stats.py's
    `substitution`, so every planned cell still holds exactly one row;
  * the POOL counts the 54 + 54 rows of the two substituted runs over the same 18 instances (6 rows per instance).
    The interval is still a bootstrap over the 18 instances, so pooling shrinks only the run-to-run part of the
    uncertainty, which is small here, not the part from having sampled these 18 issues.

NOT ONE CONFIGURATION IN ONE RESPECT: the gateway's retry window for a rate-limited call was 9 tries when sweep-4 ran
and 21 when sweep-6 ran (`RETRY_WINDOW`, held against `gateway/models.toml` at each manifest sha by a test), and the
waits count against the agent's wall budget. Under 9 tries a long spell ends a task as a harness error, which is then
re-run; under 21 the task carries on and is scored as an agent result. So the pool and the paired comparison mix two
retry policies, and every note below says so.

The paired comparison is exploratory and cannot be strong: the two runs are meant as one configuration, so there is no
pre-registered comparison, and with only a handful of discordant instances the exact tests cannot reach a small p.
"""

import itertools
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stats as S  # noqa: E402 -- the analysis directory is not a package

OUT = S.DATA / "replication.json"
#: gateway `max_attempts` at each run's manifest sha (f02ccb3 for sweep-4/4b, 54cf18c for sweep-6/6b).
RETRY_WINDOW = {"sweep-4": 9, "sweep-6": 21}
CONFOUND = ("sweep-4 ran with a 9-try retry window for rate-limited calls and sweep-6 with 21; the waits count against "
            "the agent's hour, so this mixes two retry policies.")


def per_instance(cells):
    """instance -> (passes, rows) over a {(instance, run_index or tag): row} mapping."""
    acc = defaultdict(lambda: [0, 0])
    for (inst, _), row in cells.items():
        acc[inst][0] += S.passed(row)
        acc[inst][1] += 1
    return {k: tuple(v) for k, v in sorted(acc.items())}


def figure(label, cells, note, rng_label=None):
    pi = per_instance(cells)
    k, n = sum(p for p, _ in pi.values()), sum(m for _, m in pi.values())
    ci = S.boot_ci(pi, S.rng_for(rng_label or label), S.rate_of)
    return {"label": label, "passed": k, "planned": n, "rate": k / n, "instances": len(pi),
            "ci_bootstrap_instances": [ci["lo"], ci["hi"]], "note": note}


def exact_sign_flip(diffs):
    """Exact two-sided sign-flip p over the nonzero differences (zeros flip to themselves)."""
    nz = [d for d in diffs if d]
    if not nz:
        return 1.0
    obs = abs(sum(nz))
    hits = sum(abs(sum(s * d for s, d in zip(signs, nz, strict=True))) >= obs - 1e-12
               for signs in itertools.product((1, -1), repeat=len(nz)))
    return hits / 2 ** len(nz)


def paired(cells4, cells6, basis):
    p4, p6 = per_instance(cells4), per_instance(cells6)
    diffs = [p6[i][0] - p4[i][0] for i in p4]
    b = sum(S.passed(cells4[(i, 0)]) and not S.passed(cells6[(i, 0)]) for i in p4)
    c = sum(S.passed(cells6[(i, 0)]) and not S.passed(cells4[(i, 0)]) for i in p4)
    return {
        "basis": basis, "exploratory": True,
        "same": sum(d == 0 for d in diffs), "favour_sweep_4": sum(d < 0 for d in diffs), "favour_sweep_6": sum(d > 0 for d in diffs),
        "net_passes_sweep_6_minus_sweep_4": sum(diffs),
        "pass_every_run_in_both": sum(p4[i][0] == p4[i][1] and p6[i][0] == p6[i][1] for i in p4),
        "pass_no_run_in_both": sum(p4[i][0] == 0 and p6[i][0] == 0 for i in p4),
        "sign_flip_exact_p_two_sided": exact_sign_flip(diffs),
        "smallest_p_possible_with_these_discordant_instances": 2 / 2 ** sum(1 for d in diffs if d) if any(diffs) else 1.0,
        "mcnemar_run_index_0": {"sweep_4_only": b, "sweep_6_only": c, "p_exact_two_sided": S.mcnemar_exact(b, c)},
        "by_instance": {i: {"sweep-4": p4[i][0], "sweep-6": p6[i][0], "runs": p4[i][1]} for i in p4},
        "note": CONFOUND,
    }


def cost_view(cells, passes_of=None):
    """sweep-4's published definitions: over the final rows of the grid (one per cell)."""
    rows = [r for r in cells.values() if r is not None]
    costs = [r["cost"] for r in rows]
    lat = [r["latency_s"] for r in rows if r["latency_s"] is not None]
    passes = sum(S.passed(r) for r in rows)
    return {"rows": len(rows), "mean_cost_usd": statistics.fmean(costs), "median_cost_usd": statistics.median(costs),
            "total_cost_usd": sum(costs), "passes": passes, "cost_per_pass_usd": sum(costs) / passes if passes else None,
            "median_latency_s": statistics.median(lat),
            "stop_reasons": dict(sorted(Counter(r["stop_reason"] or "-" for r in rows if r["status"] == "completed").items()))}


def build():
    s4, s4b, s6, s6b = (S.load_run(r) for r in ("sweep-4", "sweep-4b", "sweep-6", "sweep-6b"))
    g4, g6 = S.build_grid(s4), S.build_grid(s6)
    sub4, cells4 = S.substitution(s4, s4b)
    sub6, cells6 = S.substitution(s6, s6b)
    pooled = {(i, f"sweep-4#{k}"): r for (i, k), r in sub4.items()} | {(i, f"sweep-6#{k}"): r for (i, k), r in sub6.items()}
    figures = [
        figure("sweep-6 official", g6, "As `repolace-eval report` computes it: every planned row that did not pass is a non-pass."),
        figure("sweep-6 substituted", sub6, "Hand-stated substitution: the rows sweep-6 lost to Vertex 429 replaced by their sweep-6b re-runs."),
        # The bare seed, as stats.py uses for its "baseline": the same stream, so the same interval to the last bit.
        figure("sweep-4 substituted (baseline)", sub4, "The published baseline, recomputed here as a check.", rng_label="baseline"),
        figure("pooled 4+6 substituted", pooled, "Hand pool of the two substituted runs: 6 rows per instance. " + CONFOUND),
    ]
    spend = {r: sum(x["cost"] for x in d["rows"]) for r, d in (("sweep-6", s6), ("sweep-6b", s6b))}
    return {
        "runs": {"git_sha": {"sweep-4": s4["manifest"].get("git_sha"), "sweep-6": s6["manifest"].get("git_sha")},
                 "model": s6["manifest"].get("model"),
                 "retry_window_max_attempts": RETRY_WINDOW,
                 "substituted_cells": {"sweep-4": [f"{i}#{k}" for i, k in cells4], "sweep-6": [f"{i}#{k}" for i, k in cells6]}},
        "figures": figures,
        "paired": {"substituted": paired(sub4, sub6, "the two hand-substituted grids"),
                   "official": paired(g4, g6, "the two official grids (lost rows count as non-passes)")},
        "cost": {"definition": "sweep-4's published one: over the 54 final rows of the substituted grid, one row per cell",
                 "sweep-6 substituted": cost_view(sub6), "sweep-4 substituted (baseline)": cost_view(sub4),
                 "spend_usd": spend | {"total": sum(spend.values())}},
    }


def main():
    report = build()
    OUT.write_text(json.dumps(report, indent=1) + "\n")
    for f in report["figures"]:
        lo, hi = f["ci_bootstrap_instances"]
        print(f"{f['label']:32s} {f['passed']:>3}/{f['planned']:<3} {100 * f['rate']:5.1f}%  CI {100 * lo:.1f}-{100 * hi:.1f}"
              f"  width {100 * (hi - lo):.1f}", file=sys.stderr)
    for basis, p in report["paired"].items():
        m = p["mcnemar_run_index_0"]
        print(f"paired ({basis}): {p['same']} same, {p['favour_sweep_4']} favour 4, {p['favour_sweep_6']} favour 6, net "
              f"{p['net_passes_sweep_6_minus_sweep_4']}, all-pass {p['pass_every_run_in_both']}, none {p['pass_no_run_in_both']}, "
              f"exact p {p['sign_flip_exact_p_two_sided']:.3f} (min {p['smallest_p_possible_with_these_discordant_instances']:.3f}), "
              f"McNemar r0 {m['sweep_4_only']}:{m['sweep_6_only']} p {m['p_exact_two_sided']:.3f}", file=sys.stderr)
    for k in ("sweep-6 substituted", "sweep-4 substituted (baseline)"):
        c = report["cost"][k]
        print(f"{k}: mean ${c['mean_cost_usd']:.3f} median ${c['median_cost_usd']:.3f} per pass ${c['cost_per_pass_usd']:.2f} "
              f"({c['total_cost_usd']:.2f}/{c['passes']}) median {c['median_latency_s']:.0f} s stops {c['stop_reasons']}", file=sys.stderr)
    print(f"spend {report['cost']['spend_usd']}", file=sys.stderr)


if __name__ == "__main__":
    main()
