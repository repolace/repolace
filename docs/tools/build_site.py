"""Build the public documentation pages (served at /documentation/) from the Markdown docs.

    python docs/tools/build_site.py           write api/repolace_api/site/docs_site/
    python docs/tools/build_site.py --check   exit 1 if the committed output is stale

Only the pages in PUBLIC_PAGES are published. A link from one public page to another is rewritten to its
/documentation/ URL; a link to anything else (operations, reference, the archive, the repository root) loses its
href and keeps its text, and a table row that exists only to link to such a page is dropped, so no page
carries a dead link.

The site's Content-Security-Policy forbids inline script and style and remote resources, so the generated HTML
carries none (`_check_markup` fails the build if it finds one), and `docs_site/docs.css` and `docs.js` are
the only code, served from the site itself. `markdown` is a dev-only dependency (the `docs` dependency group)
and is imported only here, never by the api package.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
OUT = ROOT / "api" / "repolace_api" / "site" / "docs_site"
BASE = "/documentation"

#: (source file under docs/, directory slug under /documentation/, sidebar label). The first is the index.
PUBLIC_PAGES = [
    ("README.md", "", "Overview"),
    ("architecture.md", "architecture", "Architecture"),
    ("getting-started.md", "getting-started", "Getting started"),
    ("demo.md", "demo", "The demo run"),
    ("benchmark.md", "benchmark", "Benchmark"),
    ("security.md", "security", "Security"),
]

FENCE = re.compile(r"^\s*(```|~~~)")
TABLE_FIRST_LINK = re.compile(r"^\|\s*\[[^\]]*\]\(([^)\s]+)[^)]*\)\s*\|")
A_TAG = re.compile(r"<a ([^>]*?)href=\"([^\"]*)\"([^>]*)>(.*?)</a>", re.DOTALL)
IMG_TAG = re.compile(r"<img alt=\"([^\"]*)\" src=\"([^\"]*)\"([^>]*?)\s*/?>")
FORBIDDEN = [
    (re.compile(r"<script", re.IGNORECASE), "a script tag"),
    (re.compile(r"\sstyle\s*=", re.IGNORECASE), "a style attribute"),
    (re.compile(r"\son[a-z]+\s*=", re.IGNORECASE), "an event-handler attribute"),
    (re.compile(r"""(?:src|srcset)\s*=\s*["']?(?:https?:)?//""", re.IGNORECASE), "a remote resource"),
    (re.compile(r"<(?:iframe|object|embed|link|style)\b", re.IGNORECASE), "an embedding or stylesheet element"),
]


def _github_slug(value: str, separator: str) -> str:
    """GitHub's heading anchor (lowercase, punctuation dropped, spaces to hyphens), so source links keep working."""
    text = re.sub(r"[^\w\- ]", "", re.sub(r"<[^>]*>", "", value).strip().lower())
    return re.sub(r"\s", separator, text)


def _load_markdown():
    try:
        import markdown
    except ImportError:
        sys.exit("build_site.py needs the `markdown` package: run `uv run --group docs python docs/tools/build_site.py`")
    return markdown


def _pages() -> list[tuple[Path, str, str]]:
    found = []
    for name, slug, label in PUBLIC_PAGES:
        path = DOCS / name
        if path.is_file():
            found.append((path, slug, label))
        else:
            print(f"note: docs/{name} does not exist yet, skipped")
    return found


def _url(slug: str) -> str:
    return f"{BASE}/{slug}/" if slug else f"{BASE}/"


def _resolve(source: Path, href: str, pages: dict[Path, str]) -> str | None:
    """The site URL for a relative link in `source`, or None when the target is not published."""
    if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", href, re.IGNORECASE) or href.startswith("#"):
        return href
    path, _, fragment = href.partition("#")
    target = (source.parent / path).resolve()
    suffix = f"#{fragment}" if fragment else ""
    if target in pages:
        return _url(pages[target]) + suffix
    if target.parent == (DOCS / "img").resolve() and target.is_file():
        return f"{BASE}/img/{target.name}"
    return None


def _drop_dead_rows(text: str, source: Path, pages: dict[Path, str]) -> str:
    """Remove table rows whose first cell links to an unpublished page, and a section left with no table rows."""
    sections: list[list[str]] = [[]]
    for line in text.splitlines():
        if re.match(r"^#{2,6}\s", line) and sections[-1] != []:
            sections.append([])
        sections[-1].append(line)
    out: list[str] = []
    for lines in sections:
        fenced, had_rows, kept_rows, kept = False, False, False, []
        for line in lines:
            if FENCE.match(line):
                fenced = not fenced
            match = None if fenced else TABLE_FIRST_LINK.match(line)
            if match:
                had_rows = True
                if _resolve(source, match.group(1), pages) is None:
                    continue
                kept_rows = True
            kept.append(line)
        if had_rows and not kept_rows:
            continue  # every row linked somewhere unpublished: the whole section would be an empty table
        out.extend(kept)
    return "\n".join(out) + "\n"


def _rewrite(body: str, source: Path, pages: dict[Path, str]) -> str:
    def anchor(match: re.Match) -> str:
        before, href, after, inner = match.groups()
        target = _resolve(source, html.unescape(href), pages)
        if target is None:
            return inner
        extra = ' rel="noopener noreferrer"' if re.match(r"https?://", target) else ""
        return f'<a {before}href="{html.escape(target)}"{after}{extra}>{inner}</a>'

    def image(match: re.Match) -> str:
        alt, src, rest = match.groups()
        target = _resolve(source, html.unescape(src), pages)
        if target is None or re.match(r"https?:", target):
            return ""
        return f'<a class="doc-fig" href="{html.escape(target)}"><img alt="{alt}" src="{html.escape(target)}"{rest} loading="lazy"></a>'

    body = IMG_TAG.sub(image, body)
    body = A_TAG.sub(anchor, body)
    # python-markdown writes `style="text-align: ..."` for aligned table columns, which the CSP forbids.
    body = re.sub(r"\s+style=\"text-align:\s*\w+;?\"", "", body)
    body = re.sub(r"<table>", '<div class="doc-table"><table>', body)
    return body.replace("</table>", "</table></div>")


def _check_markup(slug: str, body: str) -> None:
    for pattern, what in FORBIDDEN:
        if match := pattern.search(body):
            raise SystemExit(f"{slug or 'index'}: the page contains {what} ({match.group(0)!r}); the site CSP forbids it")


def _plain(fragment: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", fragment))).strip()


def _toc(tokens: list[dict]) -> str:
    items = "".join(f'<li><a href="#{html.escape(t["id"])}">{html.escape(t["name"])}</a></li>' for t in tokens)
    return f'<ol class="doc-toc">{items}</ol>' if items else ""


def _nav(pages: list[tuple[Path, str, str]], current: str, toc: str, *, idp: str) -> str:
    rows = []
    for _, slug, label in pages:
        here = slug == current
        mark = ' aria-current="page"' if here else ""
        rows.append(f'<li><a href="{_url(slug)}"{mark}>{html.escape(label)}</a>{toc if here else ""}</li>')
    return f'<ul class="doc-list" id="{idp}">{"".join(rows)}</ul>'


SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{full_title}</title>
<meta name="description" content="{description}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="repolace documentation">
<meta property="og:title" content="{full_title}">
<meta property="og:description" content="{description}">
<meta name="twitter:card" content="summary">
<meta name="color-scheme" content="light dark">
<link rel="preload" href="/benchmark/fonts/fraunces-latin-opsz-normal.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="/benchmark/fonts/geist-latin-wght-normal.woff2" as="font" type="font/woff2" crossorigin>
<link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon.png">
<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
<link rel="stylesheet" href="{base}/docs.css">
<script src="/benchmark/js/theme-init.js"></script>
</head>
<body>
<a class="skip" href="#main">Skip to the page</a>
<header class="topbar">
  <a class="mark" href="/" title="repolace home"><span class="wm">repolace</span> <span class="wm-sub">docs</span></a>
  <nav aria-label="Site">
    <a href="/">Home</a>
    <a href="/benchmark/">Benchmark</a>
    <a href="/pricing">Pricing</a>
    <a href="{base}/" aria-current="true">Docs</a>
  </nav>
  <details class="menu">
    <summary>Menu</summary>
    <div class="panel">
      <a href="/">Home</a>
      <a href="/benchmark/">Benchmark</a>
      <a href="/retrieval-benchmark/">Retrieval benchmark</a>
      <a href="/demo/">Demo</a>
      <a href="/pricing">Pricing</a>
      <a href="/waitlist">Join the waitlist</a>
      <a href="{base}/">Documentation</a>
    </div>
  </details>
  <button class="iconbtn" id="theme" type="button" aria-label="Switch colour theme" title="Switch colour theme">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.5 14.2A8.5 8.5 0 0 1 9.8 3.5a8.5 8.5 0 1 0 10.7 10.7Z"/></svg>
  </button>
</header>
<div class="doc-wrap">
  <aside class="doc-side">
    <nav aria-label="Documentation">
      <p class="doc-side-h">Documentation</p>
      {side}
    </nav>
  </aside>
  <details class="doc-top">
    <summary>Documentation pages</summary>
    <nav aria-label="Documentation, compact">{top}</nav>
  </details>
  <main id="main" class="doc">
{body}
{pager}
  </main>
</div>
<footer class="foot">
  <div class="wrap footrow">
    <p>repolace is in early access. Python repositories only, for now. Open source under the <a href="https://github.com/repolace/repolace/blob/main/LICENSE">MIT licence</a>.</p>
    <nav aria-label="Footer"><a href="/">Home</a><a href="/pricing">Pricing</a><a href="/waitlist">Waitlist</a><a href="/benchmark/">Benchmark</a><a href="/retrieval-benchmark/">Retrieval benchmark</a><a href="/demo/">Demo</a><a href="{base}/">Docs</a><a href="https://github.com/repolace/repolace">Source</a></nav>
  </div>
</footer>
<script src="{base}/docs.js" defer></script>
</body>
</html>
"""


def render(markdown_module) -> dict[Path, bytes]:
    pages = _pages()
    index = {path.resolve(): slug for path, slug, _ in pages}
    files: dict[Path, bytes] = {}
    for position, (path, slug, label) in enumerate(pages):
        text = _drop_dead_rows(path.read_text(encoding="utf-8"), path, index)
        md = markdown_module.Markdown(
            extensions=["tables", "fenced_code", "toc", "sane_lists"],
            extension_configs={"toc": {"slugify": _github_slug, "permalink": "#", "permalink_title": "Link to this section", "toc_depth": "2-3"}},
        )
        body = _rewrite(md.convert(text), path, index)
        _check_markup(slug, body)
        title = _plain(re.search(r"<h1[^>]*>(.*?)</h1>", body, re.DOTALL).group(1)).removesuffix("#").strip() if "<h1" in body else label
        first = re.search(r"<p>(.*?)</p>", body, re.DOTALL)
        description = html.escape(_plain(first.group(1))[:200], quote=True) if first else ""
        toc = _toc(md.toc_tokens)
        prev_link = next_link = ""
        if position > 0:
            _, s, name = pages[position - 1]
            prev_link = f'<a rel="prev" href="{_url(s)}"><span>Previous</span>{html.escape(name)}</a>'
        if position < len(pages) - 1:
            _, s, name = pages[position + 1]
            next_link = f'<a rel="next" href="{_url(s)}"><span>Next</span>{html.escape(name)}</a>'
        pager = f'<nav class="doc-pager" aria-label="Pages">{prev_link}{next_link}</nav>' if prev_link or next_link else ""
        page = SHELL.format(
            title=html.escape(title), full_title=html.escape(title if title.lower() == "repolace documentation" else f"{title} - repolace documentation"), description=description, base=BASE, body=body, pager=pager,
            side=_nav(pages, slug, toc, idp="doc-side-list"), top=_nav(pages, slug, "", idp="doc-top-list"),
        )
        files[Path(slug) / "index.html" if slug else Path("index.html")] = page.encode()
    for svg in sorted((DOCS / "img").glob("*.svg")):
        files[Path("img") / svg.name] = svg.read_bytes()
    return files


def _existing() -> set[Path]:
    keep = {"docs.css", "docs.js"}
    return {p.relative_to(OUT) for p in OUT.rglob("*") if p.is_file() and p.name not in keep} if OUT.exists() else set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed output is stale")
    args = parser.parse_args(argv)
    files = render(_load_markdown())
    if args.check:
        stale = sorted(str(p) for p in files if not (OUT / p).is_file() or (OUT / p).read_bytes() != files[p])
        stale += sorted(f"{p} (no longer generated)" for p in _existing() - set(files))
        if stale:
            print("docs_site is stale; run `python docs/tools/build_site.py`:\n  " + "\n  ".join(stale))
            return 1
        print(f"docs_site is up to date ({len(files)} files)")
        return 0
    for old in _existing() - set(files):
        (OUT / old).unlink()
    for rel, data in files.items():
        target = OUT / PurePosixPath(rel.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for directory in sorted((p for p in OUT.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            shutil.rmtree(directory)
    print(f"wrote {len(files)} files to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
