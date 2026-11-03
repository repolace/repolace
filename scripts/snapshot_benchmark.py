"""Refresh the public site's numbers snapshot (the landing and pricing pages) from BENCHMARK/.

The site package carries its own copy so the internet-facing process never reads `eval/`, the database or
the live sweep pages. The /benchmark report itself is the static site in `site/benchmark_site/`, refreshed by hand. Run after `BENCHMARK/analysis/analyze.py`; `api/tests/test_site_pages.py` fails if the copies drift.
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "api" / "repolace_api" / "site"


def main() -> None:
    (SITE / "data").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "BENCHMARK" / "analysis" / "results.json", SITE / "data" / "results.json")
    print("snapshot refreshed")


if __name__ == "__main__":
    main()
