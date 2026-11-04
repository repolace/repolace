# Sample ready

`report.sample.json` holds sweep-4, sweep-4b and the baseline substitution (35/54, 44.4-83.3% over instances). It has the
same shape as `report.json` except that `comparisons`, `difficulty`, `cost_effectiveness`, `cost_curves` and `retrieval`
are empty/null. Build against it; `report.json` (all runs) is also in this directory and `report.schema.json` describes both.
Regenerate with `uv run --all-packages python BENCHMARK/analysis/stats.py [--sample]`.
