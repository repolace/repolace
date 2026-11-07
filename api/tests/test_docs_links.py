"""Every relative link and image in the repository's Markdown docs points at a file that exists.

Covers README.md, CONTRIBUTING.md, SECURITY.md and docs/**/*.md, except the two archived documents.
http(s), mailto and bare `#anchor` links to other places are ignored; an `#anchor` on a Markdown target is
checked against that file's headings (GitHub's slug rules, simplified).
"""

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
ARCHIVED = {"docs/INTERVIEW_GUIDE.md", "docs/PHASE1_TESTING.md"}
LINK = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
FENCE = re.compile(r"^\s*(```|~~~)")


def _docs() -> list[Path]:
    found = [ROOT / n for n in ("README.md", "CONTRIBUTING.md", "SECURITY.md")]
    found += sorted((ROOT / "docs").rglob("*.md"))
    return [p for p in found if p.relative_to(ROOT).as_posix() not in ARCHIVED]


def _prose(path: Path) -> list[str]:
    """Lines outside fenced code blocks, with inline code removed."""
    lines, fenced = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            lines.append(re.sub(r"`[^`]*`", "", line))
    return lines


def _slug(heading: str) -> str:
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for line in _prose(path):
        if m := re.match(r"^#{1,6}\s+(.*?)\s*#*$", line):
            slug = _slug(m.group(1))
            n = seen.get(slug, 0)
            seen[slug] = n + 1
            out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def _problems(path: Path) -> list[str]:
    bad = []
    for line in _prose(path):
        for match in LINK.finditer(line):
            target = match.group(1).strip("<>")
            if re.match(r"^([a-z][a-z0-9+.-]*:|//)", target, re.IGNORECASE):
                continue  # http(s), mailto, any other scheme
            ref, _, anchor = target.partition("#")
            dest = path if not ref else (path.parent / unquote(ref)).resolve()
            if not dest.exists():
                bad.append(f"{path.relative_to(ROOT)}: {target} -> missing file")
            elif anchor and dest.suffix == ".md" and dest.is_file() and unquote(anchor).lower() not in _anchors(dest):
                bad.append(f"{path.relative_to(ROOT)}: {target} -> no such heading")
    return bad


def test_readme_exists() -> None:
    assert (ROOT / "README.md").is_file()


def test_relative_links_resolve() -> None:
    problems = [p for doc in _docs() for p in _problems(doc)]
    assert not problems, "broken links:\n" + "\n".join(problems)
