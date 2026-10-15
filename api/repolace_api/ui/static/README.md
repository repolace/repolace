# Vendored static assets

Nothing here is fetched at runtime: the UI is served same-origin with a
`default-src 'self'` Content-Security-Policy, so every asset is a file in this
directory. No node build exists; to upgrade an asset, download the pinned URL
for the new version, update this table and the template references together.

| file | what | version | source | sha256 |
|---|---|---|---|---|
| `vendor/htmx.min.js` | htmx | 2.0.4 | https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `fonts/ibm-plex-sans-latin-{400,500,600}.woff2` | IBM Plex Sans, latin subset | @fontsource/ibm-plex-sans 5.1.0 | https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.1.0/files/ibm-plex-sans-latin-{400,500,600}-normal.woff2 | see `sha256sum` |
| `fonts/ibm-plex-mono-latin-{400,500,600}.woff2` | IBM Plex Mono, latin subset | @fontsource/ibm-plex-mono 5.1.0 | https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-mono@5.1.0/files/ibm-plex-mono-latin-{400,500,600}-normal.woff2 | see `sha256sum` |
| `fonts/OFL.txt` | SIL Open Font License 1.1 text for the fonts | @fontsource/ibm-plex-sans 5.1.0 | https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.1.0/LICENSE | |

htmx is configured with `allowEval: false` and `includeIndicatorStyles: false`
(a `<meta name="htmx-config">` in the base template): the first keeps it from
using `new Function`, the second keeps it from injecting an inline `<style>`,
which the CSP would block.

Only the latin subset is shipped. U+2192 (the right arrow) is not in it, so the
templates draw arrows as inline SVG. Other glyphs outside the subset fall back
to the system font.
