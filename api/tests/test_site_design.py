"""The public site shares its design system with the benchmark report: tokens, fonts, theme, and links between them."""

import re
from pathlib import Path

import httpx
import pytest
from repolace_api.config import SiteSettings
from repolace_api.site import create_site_app

HOST = "repolace.example.test"
SITE = Path(__file__).resolve().parents[1] / "repolace_api" / "site"
REPORT_CSS = (SITE / "benchmark_site" / "style.css").read_text()
SITE_CSS = (SITE / "static" / "site.css").read_text()

pytestmark = pytest.mark.anyio

#: The tokens both stylesheets must agree on. The report's chart-series and layout tokens (--s-*, --stage*,
#: --on-fill, --nav-h) are deliberately not shared and not compared.
SHARED = [
    "--page", "--surface", "--ink", "--ink-2", "--muted", "--grid", "--axis", "--border", "--wash",
    "--accent", "--accent-ink", "--shadow", "--serif", "--sans", "--mono", "--gutter", "--ease",
]
CONTEXTS = {
    "light": re.compile(r"^:root \{(.*?)^\}", re.DOTALL | re.MULTILINE),
    "dark (media query)": re.compile(r'@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme="light"\]\) \{(.*?)^  \}', re.DOTALL | re.MULTILINE),
    "dark (attribute)": re.compile(r'^:root\[data-theme="dark"\] \{(.*?)^\}', re.DOTALL | re.MULTILINE),
}


def declarations(css: str, context: str) -> dict[str, str]:
    match = CONTEXTS[context].search(css)
    assert match, f"{context} block not found"
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+):\s*([^;]+);", match.group(1))}


@pytest.mark.parametrize("context", list(CONTEXTS))
def test_shared_tokens_are_identical_in_the_site_and_the_report(context):
    report, site = declarations(REPORT_CSS, context), declarations(SITE_CSS, context)
    compared = 0
    for name in SHARED:
        assert site.get(name) == report.get(name), f"{name} differs in {context}: site={site.get(name)!r} report={report.get(name)!r}"
        compared += name in report
    assert compared >= (17 if context == "light" else 10), f"only {compared} tokens compared in {context}"


def test_theme_is_shared_between_the_site_and_the_report():
    site_init, report_init = (SITE / "static" / "theme-init.js"), (SITE / "benchmark_site" / "js" / "theme-init.js")
    assert site_init.read_bytes() == report_init.read_bytes()
    assert 'localStorage.setItem("theme"' in (SITE / "static" / "site.js").read_text()
    assert 'localStorage.setItem("theme"' in (SITE / "benchmark_site" / "js" / "core.js").read_text()
    assert "repolace-theme" not in SITE_CSS + (SITE / "static" / "site.js").read_text()


def test_no_ibm_plex_left_and_fonts_come_from_the_reports_files():
    texts = [SITE_CSS] + [p.read_text() for p in (SITE / "templates").glob("*.html")]
    assert not any("IBM" in t or "Plex" in t for t in texts)
    assert not (SITE / "static" / "theme.js").exists()
    urls = re.findall(r'url\("(/benchmark/fonts/[^"]+)"\)', SITE_CSS)
    assert len(urls) == 3
    for url in urls:
        assert (SITE / "benchmark_site" / url.removeprefix("/benchmark/")).is_file(), url


def client() -> httpx.AsyncClient:
    app = create_site_app(SiteSettings(site_allowed_hosts=HOST, _env_file=None))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}")


async def test_every_font_and_asset_the_pages_use_is_served():
    async with client() as c:
        css_urls = re.findall(r'url\("([^"]+)"\)', SITE_CSS)
        page = (await c.get("/")).text
        assets = re.findall(r'(?:href|src)="(/(?:assets|benchmark)/[^"]+)"', page)
        for url in {*css_urls, *assets}:
            resp = await c.get(url)
            assert resp.status_code == 200, url


async def test_pages_share_one_top_bar_and_reach_each_other():
    async with client() as c:
        for path in ("/", "/pricing", "/waitlist", "/privacy", "/terms"):
            html = (await c.get(path)).text
            assert 'class="topbar"' in html and 'id="theme"' in html, path
            assert 'href="/benchmark/"' in html and 'href="/pricing"' in html and 'href="/waitlist"' in html, path
        report = (await c.get("/benchmark/")).text
        for link in ('href="/"', 'href="/pricing"', 'href="/waitlist"'):
            assert link in report, link
        assert 'href="/benchmark/"' in (await c.get("/")).text


async def test_hero_stagger_uses_classes_not_inline_styles():
    async with client() as c:
        html = (await c.get("/")).text
    assert "rise-1" in html and " style=" not in html
    for name in ("rise-1", "rise-2", "rise-3", "rise-4"):
        assert f".{name}" in SITE_CSS


async def test_site_assets_are_versioned_so_a_deploy_is_not_hidden_by_browser_caches():
    async with client() as c:
        first = (await c.get("/")).text
        second = (await c.get("/pricing")).text
    versions = set(re.findall(r"/assets/(?:site\.css|site\.js|theme-init\.js)\?v=([0-9a-f]{10})", first + second))
    assert len(versions) == 1, versions
    assert len(re.findall(r"\?v=", first)) == 3
