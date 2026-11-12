"""The demo replay at /demo/: served, within the site's CSP, drawn only from its recorded data, and free of private details.

The data is written by `docs/tools/export_demo.py` from one real task and committed under `repolace_api/site/demo_site/`.
"""

import json
import re
from pathlib import Path

import httpx
import pytest
from repolace_api.config import SiteSettings
from repolace_api.site import create_site_app

HOST = "repolace.example.test"
ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "api" / "repolace_api" / "site"
DEMO = SITE / "demo_site"
DATA = DEMO / "data" / "demo.json"
SCHEMA = DEMO / "data" / "demo.schema.json"
VIDEO = DEMO / "clips" / "demo-run.mp4"
POSTER = DEMO / "clips" / "demo-run-poster.jpg"
TASK_ID = "4f381d8e-ef4f-4ba4-be31-1c513f9d2e3b"

pytestmark = pytest.mark.anyio


def site_client() -> httpx.AsyncClient:
    settings = SiteSettings(site_allowed_hosts=HOST, _env_file=None)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_site_app(settings)), base_url=f"http://{HOST}")


def load() -> dict:
    return json.loads(DATA.read_text())


def text_files() -> list[Path]:
    return sorted(p for p in DEMO.rglob("*") if p.is_file() and p.suffix in {".html", ".js", ".css", ".json"})


async def test_demo_page_data_and_clips_are_served():
    async with site_client() as c:
        redirect = await c.get("/demo")
        page = await c.get("/demo/")
        data = await c.get("/demo/data/demo.json")
        schema = await c.get("/demo/data/demo.schema.json")
        video = await c.get("/demo/clips/demo-run.mp4")
        poster = await c.get("/demo/clips/demo-run-poster.jpg")
        docs = await c.get("/docs")
    assert redirect.status_code in (301, 302, 307, 308) and redirect.headers["location"].endswith("/demo/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert data.status_code == 200 and data.json() == load() and schema.status_code == 200
    assert video.status_code == 200 and video.content[4:8] == b"ftyp" and "video/mp4" in video.headers["content-type"]
    assert poster.status_code == 200 and poster.content[:3] == b"\xff\xd8\xff"
    assert docs.status_code == 404


def test_page_structure_and_disclosure():
    html = (DEMO / "index.html").read_text()
    assert len(re.findall(r"<h1[\s>]", html)) == 1
    assert re.search(r"<title>[^<]+</title>", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text.replace("&ldquo;", '"').replace("&rdquo;", '"').replace("&rsquo;", "'"))
    for phrase in (
        "This is one run on a toy repository that was built to show the flow",
        "It is not a success rate",
        "29 of 54 on 18 real SWE-bench Verified issues",
        "the first attempt at this issue",
        "nothing was re-run or picked from several tries",
        "it is not a live screen recording",
        "After that first attempt the issue was run a second time",
        "there was no third run",
        'the repository had no failing test for this issue',
        "That sentence is wrong for this run",
        "known defect",
        'passed (uncurated)',
        "checked by hand",
    ):
        assert phrase in text, phrase
    assert "—" not in html and "—" not in "".join(p.read_text() for p in (DEMO / "js").glob("*.js"))
    # the sections come in the specified order
    ids = [html.index(f'id="{i}"') for i in ("headline", "disclosure", "issue", "stages", "tests", "diff", "calls", "clips", "scope", "links")]
    assert ids == sorted(ids)
    assert 'id="hand-check"' in html


def test_page_fits_the_content_security_policy_and_loads_nothing_remote():
    html = (DEMO / "index.html").read_text()
    assert "<style" not in html and " style=" not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+\s*=", html)
    for tag, attr in re.findall(r"<(link|script|img|iframe|source|video)\b[^>]*?\b(?:src|href|srcset)=[\"']([^\"']+)", html):
        assert not re.match(r"(?:https?:)?//", attr), (tag, attr)
    for m in re.finditer(r"<a\b[^>]*\bhref=[\"'](https?://[^\"']+)", html):
        assert m.group(1).startswith("https://github.com/"), m.group(1)
    for p in [*(DEMO / "js").glob("*.js"), DEMO / "style.css"]:
        t = p.read_text()
        assert "http://" not in t.replace("http://www.w3.org/2000/svg", "") and "https://" not in t.replace("https://github.com/", ""), p
        assert 'style="' not in t and "innerHTML" not in t and "outerHTML" not in t and "insertAdjacentHTML" not in t, p
        assert "@import" not in t or (t.count("@import") == 1 and '@import url("/benchmark/style.css");' in t), p
    css = (DEMO / "style.css").read_text()
    shared = "page|surface|ink|ink-2|muted|grid|axis|border|wash|accent|accent-ink|shadow|serif|sans|mono|gutter|ease"
    assert not re.search(rf"(?<![\w-])--(?:{shared})\s*:", css) and "@font-face" not in css
    assert "/benchmark/js/theme-init.js" in html
    for ref in set(re.findall(r'"(/benchmark/[^"?]+)"', html + "".join(p.read_text() for p in (DEMO / "js").glob("*.js")))):
        assert (SITE / "benchmark_site" / ref.removeprefix("/benchmark/")).is_file(), ref


def test_clips_have_dimensions_lazy_loading_and_alt_text():
    html = (DEMO / "index.html").read_text()
    videos = re.findall(r"<video\b[^>]*>", html)
    # Autoplay-on-view without native controls: muted and playsinline are what browsers require to play without a click,
    # preload="none" keeps the file from downloading until it scrolls into view (walkthrough.js starts it).
    assert len(videos) == 1 and 'poster="clips/demo-run-poster.jpg"' in videos[0]
    for needed in ("muted", "loop", "playsinline", 'preload="none"'):
        assert needed in videos[0], needed
    assert "controls" not in videos[0]
    assert 'id="walkthrough-toggle"' in html and "Pause video" in html  # a way to stop moving content (WCAG 2.2.2)
    js_walk = (DEMO / "js" / "walkthrough.js").read_text()
    assert "prefers-reduced-motion" in js_walk and "IntersectionObserver" in js_walk and ".play()" in js_walk
    assert re.search(r'\swidth="\d+"', videos[0]) and re.search(r'\sheight="\d+"', videos[0])
    assert 'src="clips/demo-run.mp4"' in html
    assert "<img" not in html
    assert html.count('<li>') >= 7  # the transcript: the video is not the only way to read the steps


def test_walkthrough_sizes_are_capped():
    assert VIDEO.stat().st_size < 4 * 1024 * 1024 and POSTER.stat().st_size < 200 * 1024, (VIDEO.stat().st_size, POSTER.stat().st_size)
    assert sorted(p.name for p in (DEMO / "clips").iterdir()) == ["demo-run-poster.jpg", "demo-run.mp4"]


def test_data_matches_its_schema():
    schema, data = json.loads(SCHEMA.read_text()), load()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    types = {"string": str, "integer": int, "number": (int, float), "object": dict, "array": list, "boolean": bool, "null": type(None)}

    def check(value, sch, where):
        if "const" in sch:
            assert value == sch["const"], where
        t = sch.get("type")
        if isinstance(t, list):  # a union such as ["string", "null"]: the value must satisfy one member
            members = [m for m in t if isinstance(value, types[m]) and not (m in ("integer", "number") and isinstance(value, bool))]
            assert members, f"{where}: {value!r} is none of {t}"
            t = members[0]
            sch = {**sch, "type": t}
        if t:
            assert isinstance(value, types[t]) and not (t in ("integer", "number") and isinstance(value, bool)), where
        if t == "string":
            assert len(value) >= sch.get("minLength", 0), where
            if "pattern" in sch:
                assert re.search(sch["pattern"], value), where
        if t in ("integer", "number") and "minimum" in sch:
            assert value >= sch["minimum"], where
        if t == "object":
            props = sch.get("properties", {})
            for k in sch.get("required", []):
                assert k in value, f"{where}.{k} missing"
            if sch.get("additionalProperties") is False:
                assert set(value) <= set(props), f"{where}: unexpected {set(value) - set(props)}"
            for k, v in value.items():
                if k in props:
                    check(v, props[k], f"{where}.{k}")
        if t == "array":
            for i, v in enumerate(value):
                check(v, sch["items"], f"{where}[{i}]")

    check(data, schema, "$")


def test_data_arithmetic_is_consistent():
    d = load()
    assert abs(sum(c["cost_usd"] for c in d["llm_calls"]) - d["cost_usd"]) < 1e-4
    assert [c["n"] for c in d["llm_calls"]] == list(range(1, len(d["llm_calls"]) + 1))
    assert all(s["seconds"] >= 0 for s in d["stages"]) and all(r["duration_seconds"] >= 0 for r in d["test_runs"])
    assert all(c["cached_input_tokens"] <= c["input_tokens"] for c in d["llm_calls"])
    lines = d["diff"].splitlines()
    add = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    dele = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    assert (add, dele) == (16, 1)
    base, after = d["test_runs"][0], d["test_runs"][-1]
    assert (base["attempt"], base["passed_count"], base["failed_count"]) == (0, 7, 1)
    assert (after["attempt"], after["passed_count"], after["failed_count"]) == (1, 8, 0)
    assert (d["status"], d["outcome"], d["stop_reason"]) == ("pr_opened", "passed", "submitted")
    assert d["task_short_id"] == TASK_ID[:8]
    # the stages account for the run, to within the bookkeeping gaps between stages
    assert abs(sum(s["seconds"] for s in d["stages"]) - d["duration_seconds"]) < 3


def test_page_renders_every_number_from_the_data_not_from_the_source():
    """The only figures typed into the page are the disclosure's benchmark headline (the video's size attributes sit inside a tag)."""
    html = (DEMO / "index.html").read_text()
    body = re.sub(r"<(?:svg|script|style)\b.*?</(?:svg|script|style)>|<[^>]+>|&\w+;", " ", html, flags=re.DOTALL)
    allowed = {"29", "54", "18"}  # "29 of 54 on 18 real SWE-bench Verified issues"
    typed = set(re.findall(r"\d+", body)) - allowed
    assert typed == set(), typed
    js = "".join(p.read_text() for p in (DEMO / "js").glob("*.js"))
    for forbidden in ("7 passed", "8 passed", "0.292", "$0.29", "4 min", "4f381d8e"):
        assert forbidden not in js and forbidden not in body, forbidden


def test_benchmark_headline_in_the_disclosure_is_the_official_figure():
    results = json.loads((ROOT / "BENCHMARK" / "analysis" / "results.json").read_text())["headlines"]["sweep4_official"]
    html = (DEMO / "index.html").read_text()
    m = re.search(r"(\d+) of (\d+) on (\d+) real SWE-bench Verified issues", html)
    assert m and (int(m.group(1)), int(m.group(2))) == (results["passed"], results["planned"])


@pytest.mark.parametrize("path", text_files(), ids=lambda p: p.relative_to(DEMO).as_posix())
def test_nothing_private_in_the_published_files(path):
    text = path.read_text()
    for needle in ("/var/tmp", "/home/", "claimed_by", "error_message", "localhost", "@gmail", "arn:aws", TASK_ID):
        assert needle not in text, f"{needle!r} in {path.name}"
    assert not re.search(r"repolace-[0-9a-f]{12}", text), path.name
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text), path.name
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), path.name


def test_data_has_only_dates_and_counts():
    d = load()
    text = DATA.read_text()
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:", text) and not re.search(r"\d{2}:\d{2}:\d{2}", text)
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text)
    for run in d["test_runs"]:
        assert set(run) == {"attempt", "passed_count", "failed_count", "errors_count", "duration_seconds"}
    assert not any(k in d for k in ("claimed_by", "error_message", "heartbeat", "user_id", "task_id"))
    hand = DEMO / "data" / "hand_check.json"
    if hand.exists():
        assert set(json.loads(hand.read_text())) == {"text"}
