# Retrieval report (served at /retrieval-benchmark/)

A static page for the retrieval grid (`repolace-eval retrieval`: 4 index x 2 query embedding strategies on the benchmark's 18 bugs).
Mounted in `api/repolace_api/site/app.py` next to `/benchmark/`, under the same Content-Security-Policy (no inline script or style, no remote origin).

- **Data:** `data/retrieval.json`, validated by `data/retrieval.schema.json`. Written by `BENCHMARK/analysis/retrieval_stats.py`; never edit it by hand.
  - Real data: `uv run --all-packages python BENCHMARK/analysis/retrieval_stats.py eval/runs/retrieval-2/report.json` (about 30 s, 10,000 bootstrap resamples and 20,000 permutations).
  - Placeholder (aggregate-only state): `... retrieval_stats.py --placeholder`. This is the committed state until the run finishes: the aggregate grid and the intervals printed with it, with every per-bug panel marked pending.
  - Dev preview of the full page: build from `BENCHMARK/analysis/fixtures/retrieval_eval_fixture.json` (synthetic, seeded) into a temp copy of this directory. Its output has `synthetic: true` and the page shows a banner. Never commit it; a test fails if the committed file is synthetic.
- **Shared with `/benchmark/`, by absolute path:** `/benchmark/style.css` (`@import`, so the tokens, fonts and layout are the report's), `/benchmark/fonts/`, `/benchmark/js/vendor/d3.min.js` (D3 7.8.5), `/benchmark/js/theme-init.js`, `/benchmark/js/core.js`. Keep both directories mounted together.
- **Reading level:** written for someone with no context. The top of the page (what this is, the short answer, why it was run), a four-step explainer, a plain-language glossary and a "what this does not show" list sit before any chart; every chart has a one-sentence "how to read this chart" above it and a "what it shows" takeaway below it (both built from the JSON by `notes()` in `js/shared.js`). Jargon names (`truncate`, `head_tail`, ...) stay as the labels of the data, each explained in words at first use. Method-only terms (SWE-bench, p-values, formulas) live in the Method section.
- **Own files:** `index.html`, `style.css` (cell colours and the retrieval figures), `js/` (`main.js` assembly, overview, glossary and prose built from the JSON, `data.js`, `shared.js` with the cell styling, legend and `notes()`, `grid.js`, `delta.js`, `instances.js`).
- **Colour:** the four index strategies use the benchmark's four validated series hues; the query strategy is the second channel (solid mark and line = `truncate`, hollow mark and dashed line = `head_tail`).
- **Preview:** `uv run uvicorn repolace_api.site.main:app --port 8765`, then `/retrieval-benchmark/`.
- **Deploy:** rebuild the `site` compose image. That makes it public, so it needs the user's approval.
