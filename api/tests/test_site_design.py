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
    # three styles/scripts plus three icon links, all carrying the same content-hash version
    assert len(re.findall(r"\?v=", first)) == 6


async def test_every_page_and_the_report_carry_the_favicon_and_it_is_served():
    async with client() as c:
        for path in ("/", "/pricing", "/waitlist", "/privacy", "/terms", "/benchmark/"):
            html = (await c.get(path)).text
            assert 'rel="icon" type="image/svg+xml"' in html and 'rel="apple-touch-icon"' in html, path
        icon = await c.get("/assets/favicon.svg")
        touch = await c.get("/assets/apple-touch-icon.png")
        ico = await c.get("/favicon.ico")
    assert icon.status_code == 200 and "svg" in icon.headers["content-type"]
    assert touch.status_code == 200 and touch.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert ico.status_code == 200 and ico.headers["content-type"] == "image/png"


async def test_the_logo_is_the_wordmark_not_the_old_arrow_glyph():
    async with client() as c:
        home = (await c.get("/")).text
        report = (await c.get("/benchmark/")).text
    assert '<a class="mark" href="/" aria-label="repolace home">repolace</a>' in home
    assert "M5 5h9a4 4 0 0 1 0 8H8" not in home + report  # the placeholder arrow path
    assert '<span class="wm">repolace</span>' in report


def test_logo_files_are_outlined_and_self_contained():
    static = SITE / "static"
    for name in ("repolace-wordmark.svg", "repolace-wordmark-ink.svg", "repolace-wordmark-paper.svg", "favicon.svg"):
        svg = (static / name).read_text()
        assert svg.startswith("<svg") and "<text" not in svg and "font-family" not in svg, name
        assert "http://" not in svg.replace("http://www.w3.org/2000/svg", ""), name
    assert 'fill="currentColor"' in (static / "repolace-wordmark.svg").read_text()
    assert 'fill="#2a78d6"' in (static / "favicon.svg").read_text()
