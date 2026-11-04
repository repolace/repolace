# BENCHMARK

The Phase 1 headline run, preserved with its data and analysis: sweep-4 (single agent, Gemini 3.1 Pro, 18 instances x 3 runs) and sweep-4b (a re-run of the 12 rows sweep-4 lost to Vertex 429 errors). Start with [REPORT.md](REPORT.md).

| Path | What it is |
|---|---|
| `REPORT.md` | The write-up: headline and its definitions, lost rows, per-instance and per-repository results, stop reasons, step use, cost and latency, sensitivity, limits |
| `data/tasks.csv` | One row per task of both sweeps (66 rows: 54 + 12): outcome, stop reason, latency, calls, tokens, cost, scored attempts. Exported from the database |
| `data/tool_use.csv` | Per-task tool-call counts and the call number of the first edit (61 tasks with model calls). Derived from `llm_calls` |
| `data/sweep-4-report.md`, `sweep-4b-report.md` | The reports `repolace-eval report` wrote. The sweep-4 file was written mid-run (11 harness errors; the finished database has 12) |
| `data/sweep-4-manifest.json`, `sweep-4b-manifest.json` | Run manifests: git sha, model, limits, instance list, enqueue time |
| `analysis/analyze.py` | Standard-library-only script: bootstrap over instances, Wilson intervals, pass@k, ICC, Cochran's Q, Fisher exact, permutation tests, sensitivity table, figures |
| `analysis/results.json` | Every number in the report, machine-readable |
| `figures/*.svg` | Charts used in the report |

Regenerate: `python3 -I BENCHMARK/analysis/analyze.py`.

Two things to keep straight: the official sweep-4 headline is **29/54 (53.7%)**; **35/54 (64.8%)** is a hand-stated substitution of the 12 lost rows by their re-run and is not produced by `repolace-eval report`.

## The report site

`site/` is a static report covering every run (sweep-4, 4b, 5, r1, r2, e1, m1) and the retrieval grid, with the statistics in [analysis/stats.py](analysis/stats.py). It is built from `site/data/report.json`; nothing on the page is typed by hand except wording.

| Path | What it is |
|---|---|
| `site/index.html`, `style.css`, `js/` | The page: no framework, D3 7.9.0 from cdnjs, one ES-module per figure |
| `site/fonts/` | Fraunces, Geist and Geist Mono (SIL Open Font License; licences alongside) |
| `site/data/report.json`, `report.schema.json` | Every number the page shows, and its schema; `report.sample.json` is the sweep-4/4b-only sample |
| `site/data/code_versions.json` | Which gateway retry window each row ran under |
| `site/video/` | The four Manim explainers (`clip1..4.{mp4,webm,jpg}`); the page works without them |
| `site/serve.py` | Local server on 127.0.0.1 with the right types and HTTP Range support for video |
| `analysis/export_runs.py`, `analysis/stats.py` | Read-only database export, then the statistics |

Regenerate and view:

```
export DOCKER_HOST=unix:///run/user/1000/docker.sock
python3 -I BENCHMARK/analysis/export_runs.py
uv run --all-packages python BENCHMARK/analysis/stats.py
python3 BENCHMARK/site/serve.py 8771      # http://127.0.0.1:8771/
```

Deploy only the page and what it loads: `index.html`, `style.css`, `js/`, `fonts/`, `data/report.json` and the four finished clips in `video/`. `video/media/`, `video/script.py` and `video/__pycache__/` are build output and are not for upload.
