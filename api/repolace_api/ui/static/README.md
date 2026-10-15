# Vendored static assets

Nothing here is fetched at runtime: the UI is served same-origin with a
`default-src 'self'` Content-Security-Policy, so every asset is a file in this
directory. No node build exists; to upgrade an asset, download the pinned URL
for the new version, update this table and the template references together.

| file | what | version | source | sha256 |
|---|---|---|---|---|
| `vendor/htmx.min.js` | htmx | 2.0.4 | https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `fonts/ibm-plex-sans-latin-{400,500,600}.woff2` | IBM Plex Sans, latin subset | @fontsource/ibm-plex-sans 5.1.0 | https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.1.0/files/ibm-plex-sans-latin-{400,500,600}-normal.woff2 | see the font hashes below |
| `fonts/ibm-plex-mono-latin-{400,500,600}.woff2` | IBM Plex Mono, latin subset | @fontsource/ibm-plex-mono 5.1.0 | https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-mono@5.1.0/files/ibm-plex-mono-latin-{400,500,600}-normal.woff2 | see the font hashes below |
| `fonts/OFL.txt` | SIL Open Font License 1.1 text for the fonts | @fontsource/ibm-plex-sans 5.1.0 | https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.1.0/LICENSE | |

htmx is configured with `allowEval: false`, `allowScriptTags: false`, `selfRequestsOnly: true` and `includeIndicatorStyles: false`
(a `<meta name="htmx-config">` in the base template): the first keeps it from
using `new Function`, `allowScriptTags` and `selfRequestsOnly` stop a swapped response from running script or htmx from calling another origin; the last keeps it from injecting an inline `<style>`,
which the CSP would block.

Only the latin subset is shipped. U+2192 (the right arrow) is not in it, so the
templates draw arrows as inline SVG. Other glyphs outside the subset fall back
to the system font.

## Font sha256

| file | sha256 |
|---|---|
| `fonts/ibm-plex-mono-latin-400.woff2` | `3c5a451f9ec27a354b0c2bcca636c6ec17a651281aabf29f8427e210a1d31e85` |
| `fonts/ibm-plex-mono-latin-500.woff2` | `756026ff72eb76fd971ac4b7504cec55eef62109d2684c2cad8da32170b80b37` |
| `fonts/ibm-plex-mono-latin-600.woff2` | `c4d3deb734a27e6d0dc7a6b464779f70ba1c272e26287860a14e35e85acb5b76` |
| `fonts/ibm-plex-sans-latin-400.woff2` | `db71f8a28ad8501544fb4e7668e3c6d0b731760b6f20de3525ebaeba597f1922` |
| `fonts/ibm-plex-sans-latin-500.woff2` | `5ef914e59b0047a261844d96acabb60c34d3acab6b85ea24198726ce4781fd37` |
| `fonts/ibm-plex-sans-latin-600.woff2` | `31535a91ce3f6b8ed3ddedadab1e49957e2220263a640df1a3f14f6fdfe15eb6` |
