# repolace brand

**The logo is the wordmark**: lowercase `repolace` in Fraunces, weight 560, optical size 72, tracking -0.01em, ink only
(`#0b0b0b` on paper `#f9f9f7`, `#f0efec` on `#0d0d0d`). There is deliberately no icon: the marks drawn so far
(a laced-bracket mark, a check in a ring, an r-and-l ligature, the brief's "lace") were rejected because they were
either generic (a "verified" tick), unreadable at 16 px, or both. Revisit only with a concept that survives 16 px.

| File (under `api/repolace_api/site/static/`) | Use |
|---|---|
| `repolace-wordmark.svg` | Outlined word, `fill="currentColor"`: inherits the text colour |
| `repolace-wordmark-ink.svg` / `-paper.svg` | Same outlines with a fixed colour, for slides and README headers |
| `favicon.svg`, `favicon.png` (32), `apple-touch-icon.png` (180) | A paper-coloured serif `r` on the accent tile `#2a78d6`. The blue tile stays visible on light and dark tabs; a black tile vanished on a dark one |

Rules: clear space around the word is the height of the `o`; never add a tagline, italics, an underline or a two-tone
split of "repo" and "lace" (it makes the name read as two words); the colour accent belongs to the favicon tile, not the
word. In the site UI the wordmark is live text (`.mark` in `site.css`) at the same settings, because Fraunces is already
loaded; the SVG files exist for places that cannot load the font.

Regenerate with `docs/brand/build_logo.py` (its docstring has the command). The first round of concepts and the brief
that rejected the stock marks were written to `/tmp/repolace-logo/` and are not kept.
