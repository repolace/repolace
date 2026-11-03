"""Regenerate the logo files from the site's own Fraunces font. Needs fonttools and brotli, which are not project
dependencies, so run it in a throwaway environment from the repo root:

    uv run --no-project --with fonttools --with brotli python docs/brand/build_logo.py

then render the PNGs (rsvg-convert, from librsvg):

    cd api/repolace_api/site/static
    rsvg-convert -w 180 -h 180 favicon.svg -o apple-touch-icon.png
    rsvg-convert -w 32 -h 32 favicon.svg -o favicon.png

Kerning is not applied (the GPOS table is ignored); at these sizes the default advances look right.
"""
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "api" / "repolace_api" / "site"
SRC = str(SITE / "benchmark_site" / "fonts" / "fraunces-latin-opsz-normal.woff2")
OUT = SITE / "static"
font = instancer.instantiateVariableFont(TTFont(SRC), {"opsz": 72, "wght": 560})
gs, cmap, hmtx = font.getGlyphSet(), font.getBestCmap(), font["hmtx"]

def outline(text, tracking=-0.01):
    """Return (path d, (xmin,ymin,xmax,ymax)) with y flipped for SVG; tracking in em."""
    d, x, bp = [], 0, BoundsPen(gs)
    for ch in text:
        name = cmap[ord(ch)]
        pen = SVGPathPen(gs, ntos=lambda v: f"{v:.1f}".rstrip("0").rstrip("."))
        gs[name].draw(TransformPen(pen, (1, 0, 0, -1, x, 0)))
        gs[name].draw(TransformPen(bp, (1, 0, 0, 1, x, 0)))
        d.append(pen.getCommands())
        x += hmtx[name][0] + tracking * 2000
    return " ".join(d), bp.bounds

# Full word, outlined. y is flipped, so the viewBox top is -ymax.
d, (x0, y0, x1, y1) = outline("repolace")
pad = 40
vb = f"{x0 - pad:.0f} {-y1 - pad:.0f} {x1 - x0 + 2 * pad:.0f} {y1 - y0 + 2 * pad:.0f}"
w, h = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
def wordmark(fill, title="repolace"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" role="img" aria-label="{title}"><title>{title}</title>'
            f'<path fill="{fill}" d="{d}"/></svg>\n')
(OUT / "repolace-wordmark.svg").write_text(wordmark("currentColor"))
(OUT / "repolace-wordmark-ink.svg").write_text(wordmark("#0b0b0b"))
(OUT / "repolace-wordmark-paper.svg").write_text(wordmark("#f0efec"))

# Favicon: the r of the same instance, paper on an accent tile (visible on light and dark tabs). 64 grid.
rd, (rx0, ry0, rx1, ry1) = outline("r", tracking=0)
rh, rw = ry1 - ry0, rx1 - rx0
s = 40 / rh                       # glyph height 40 of 64
tx, ty = 32 - (rx0 + rw / 2) * s, 32 + (ry1 - rh / 2) * s
(OUT / "favicon.svg").write_text(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="repolace"><title>repolace</title>'
    '<rect width="64" height="64" rx="14" fill="#2a78d6"/>'
    f'<path fill="#f9f9f7" transform="translate({tx:.2f} {ty:.2f}) scale({s:.5f})" d="{rd}"/></svg>\n')
print("wordmark viewBox:", vb, " aspect %.2f" % (w / h), " r glyph h/w:", round(rh), round(rw))
