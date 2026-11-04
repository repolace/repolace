"""Writes retrieval_eval_fixture.json: SYNTHETIC data in the shape of `harness.retrieval_eval.to_json(EvalRun)`.

Nothing in the output is a measurement. It exists so retrieval_stats.py, its tests and the page can be exercised before the real run
(`eval/runs/retrieval-2/report.json`) exists. The numbers come from a seeded RNG; do not quote them, and never commit a page data file
built from them. Run: python3 BENCHMARK/analysis/fixtures/make_retrieval_fixture.py
"""

import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 2026_11_04
STRATEGIES = ("truncate", "head_tail", "windows", "signature_docstring")
QUERY_STRATEGIES = ("truncate", "head_tail")
KS = (5, 10, 20)
INSTANCES = {
    "mwaskom/seaborn": ["mwaskom__seaborn-3069", "mwaskom__seaborn-3187"],
    "pallets/flask": ["pallets__flask-5014"],
    "pylint-dev/pylint": ["pylint-dev__pylint-4551", "pylint-dev__pylint-4604", "pylint-dev__pylint-4970"],
    "pytest-dev/pytest": [f"pytest-dev__pytest-{n}" for n in (5631, 6202, 7236, 7432, 7571, 7982)],
    "sphinx-doc/sphinx": [f"sphinx-doc__sphinx-{n}" for n in (7462, 7590, 9230, 9320, 9367, 9602)],
}
#: Mean shift of the log-odds of a hit within the top 20, per cell: tiny, so the fixture shows a null result like the real one.
SHIFT = {("truncate", "truncate"): 0.0, ("head_tail", "truncate"): 0.05, ("windows", "truncate"): 0.25,
         ("signature_docstring", "truncate"): 0.3, ("truncate", "head_tail"): -0.2, ("head_tail", "head_tail"): -0.25,
         ("windows", "head_tail"): 0.1, ("signature_docstring", "head_tail"): -0.1}


def build() -> dict:
    rng = random.Random(SEED)
    results, plans = [], []
    for repo, ids in INSTANCES.items():
        plans.append({"repo": repo, "instances": len(ids), "first_index_chunks": 1000 * len(ids) + 500, "skip_reason": None})
        for iid in ids:
            hunks = rng.choice([1, 1, 2, 3])
            difficulty = rng.gauss(-0.8, 1.2)
            unreachable = rng.choice([0, 0, 0, 1, 2])
            chunk_skipped = iid == "pytest-dev__pytest-7236"  # one instance with no scorable hunk, to exercise the None path
            noise = {(s, q): rng.gauss(0, 0.9) for s in STRATEGIES for q in QUERY_STRATEGIES}
            for s in STRATEGIES:
                for q in QUERY_STRATEGIES:
                    z = difficulty + SHIFT[(s, q)] + noise[(s, q)]
                    ranks = []
                    for _ in range(hunks):
                        hit = rng.random() < 1 / (1 + 2.718281828 ** (-z))
                        ranks.append(min(20, 1 + int(rng.expovariate(0.35))) if hit else None)
                    best = min((r for r in ranks if r), default=None)
                    file_best = None if best is None else max(1, best - rng.choice([0, 0, 1, 2]))
                    if best is None and rng.random() < 0.25:
                        file_best = rng.randint(6, 20)
                    metrics = {
                        "file_recall": {str(k): (1.0 if file_best and file_best <= k else 0.0) for k in KS},
                        "chunk_recall": {str(k): (None if chunk_skipped else sum(1 for r in ranks if r and r <= k) / hunks) for k in KS},
                        "file_rr": 1 / file_best if file_best else 0.0,
                        "chunk_rr": None if chunk_skipped else (1 / best if best else 0.0),
                        "unreachable_hunks": unreachable,
                    }
                    results.append({"repo": repo, "strategy": s, "query_strategy": q, "instance_id": iid, "metrics": metrics,
                                    "chunks_written": 400 + rng.randint(0, 80)})
    return {"strategies": list(STRATEGIES), "query_strategies": list(QUERY_STRATEGIES), "plans": plans, "results": results,
            "skipped_instances": [], "dropped_targets": [["pylint-dev__pylint-4604", "tests/synthetic.py", "synthetic fixture"]],
            "_synthetic": "SYNTHETIC FIXTURE: seeded random numbers, not a measurement"}


if __name__ == "__main__":
    out = HERE / "retrieval_eval_fixture.json"
    out.write_text(json.dumps(build(), indent=1) + "\n")
    print(out)
