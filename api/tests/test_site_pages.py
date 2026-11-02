"""The public site: what it serves, what it must not serve, and the headers on every response."""

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from repolace_api.config import SiteSettings
from repolace_api.site import content, create_site_app
from repolace_api.site.security import content_security_policy

HOST = "repolace.example.test"
ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.anyio


def site_client(**kwargs) -> httpx.AsyncClient:
    settings = SiteSettings(site_allowed_hosts=HOST, _env_file=None, **kwargs)
    app = create_site_app(settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}")


@pytest.mark.parametrize("path", ["/", "/pricing", "/benchmark", "/privacy", "/terms", "/waitlist", "/waitlist/thanks"])
async def test_public_pages_render(path):
    async with site_client() as client:
        resp = await client.get(path)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<h1" in resp.text


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/ui/repos"),
        ("GET", "/ui/static/app.css"),
        ("GET", "/repos"),
        ("POST", "/repos/00000000-0000-0000-0000-000000000000/tasks"),
        ("GET", "/tasks/00000000-0000-0000-0000-000000000000"),
        ("POST", "/github/webhook"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ],
)
async def test_private_surfaces_do_not_exist_on_the_public_app(method, path):
    async with site_client() as client:
        resp = await client.request(method, path, headers={"Origin": f"http://{HOST}"})
    assert resp.status_code in (404, 405)


async def test_wrong_host_is_refused_but_health_is_not():
    async with site_client() as client:
        bad = await client.get("/", headers={"Host": "evil.example"})
        health = await client.get("/healthz", headers={"Host": "evil.example"})
    assert bad.status_code == 400
    assert health.status_code == 200


async def test_every_response_carries_security_headers_including_404():
    async with site_client() as client:
        for path in ("/", "/nope"):
            resp = await client.get(path)
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.headers["x-frame-options"] == "DENY"
            csp = resp.headers["content-security-policy"]
            assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp
            assert "frame-ancestors 'none'" in csp


def test_csp_adds_turnstile_origin_only_when_enabled():
    assert "challenges.cloudflare.com" not in content_security_policy(turnstile=False)
    assert "challenges.cloudflare.com" in content_security_policy(turnstile=True)


async def test_pages_have_no_inline_style_or_script():
    async with site_client() as client:
        for path in ("/", "/pricing", "/benchmark"):
            html = (await client.get(path)).text
            assert " style=" not in html
            assert "<style" not in html
            assert "<script>" not in html and " onclick=" not in html


async def test_landing_headline_is_the_official_figure_with_its_interval_and_caveats():
    facts = content.load_facts()
    async with site_client() as client:
        html = (await client.get("/")).text
        bench = (await client.get("/benchmark")).text
    assert f"{facts.rate:.1f}%" in html
    assert f"{facts.ci_low:.0f} to {facts.ci_high:.0f}%" in html
    assert "not a score" in html and "SWE-bench Verified" in html
    assert "not yet measured" in html
    # The hand-pooled figure must not be the landing page's number; /benchmark shows it, labelled.
    assert f"{facts.combined_rate:.1f}%" not in html
    assert "hand-stated" in bench


async def test_pricing_is_labelled_preview_and_has_no_checkout():
    async with site_client() as client:
        html = (await client.get("/pricing")).text
    assert "preview" in html.lower()
    assert "billing is not open" in html.lower() or "nobody can be charged" in html.lower()
    for name in ("Free", "Hobby", "Pro", "Team"):
        assert name in html
    assert "stripe" not in html.lower() and "checkout" not in html.lower()


async def test_benchmark_data_is_the_packaged_snapshot_and_has_no_live_data():
    async with site_client() as client:
        resp = await client.get("/benchmark/data.json")
        fig = await client.get("/assets/fig/pass_rate_intervals.svg")
    assert resp.status_code == 200
    assert resp.json() == json.loads(content.DATA_FILE.read_text())
    assert fig.status_code == 200 and "svg" in fig.headers["content-type"]
    assert "live" not in resp.json()


def test_snapshot_matches_benchmark_sources_when_present():
    src = ROOT / "BENCHMARK" / "analysis" / "results.json"
    if not src.exists():
        pytest.skip("BENCHMARK/ not in this checkout")
    assert json.loads(src.read_text()) == json.loads(content.DATA_FILE.read_text()), "run scripts/snapshot_benchmark.py"
    for svg in (ROOT / "BENCHMARK" / "figures").glob("*.svg"):
        copy = content.DATA_FILE.parent.parent / "static" / "fig" / svg.name
        assert copy.read_bytes() == svg.read_bytes(), f"{svg.name} drifted; run scripts/snapshot_benchmark.py"


def test_site_does_not_import_the_private_routers():
    code = (
        "import sys; import repolace_api.site.main; "
        "bad=[m for m in sys.modules if m.startswith(('repolace_api.routes','repolace_api.ui','repolace_api.publisher'))]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
