"""Response headers and the same-origin check for the HTML UI.

There is no authentication until Phase 4 (RBAC), so the UI is for localhost or
a private tunnel and nothing more. What this module adds is narrow:

* a Content-Security-Policy that forbids inline script, inline style, remote
  assets and framing, so an escaping mistake in a template has less to work with;
* a same-origin check on the one state-changing HTML request (enqueue), so a
  page on another site cannot make a visitor's browser POST to a repolace that
  is listening on localhost. A browser always sends `Origin` on a cross-origin
  POST; a request with neither `Origin` nor `Referer` is refused too (curl and
  scripts should use the JSON API, which this does not guard).

Limit, stated plainly: this stops cross-site request forgery. It does not stop
anyone who can reach the port from enqueueing a task with a hand-built request,
because they can send whatever `Origin` they like.
"""

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response

#: `style-src 'self'` with no `'unsafe-inline'`: the templates carry no `style=""`
#: attribute (bar widths are SVG attributes). htmx is configured not to inject
#: its indicator stylesheet. `img-src data:` is for nothing today and kept off.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

UI_PREFIX = "/ui"


def is_ui_path(path: str) -> bool:
    return path == UI_PREFIX or path.startswith(UI_PREFIX + "/")


def same_origin(request: Request) -> bool:
    """True when the request's `Origin` (else `Referer`) host equals its `Host`."""
    host = request.headers.get("host")
    if not host:
        return False
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return False
    for name in ("origin", "referer"):
        value = request.headers.get(name)
        if value is not None:
            # `Origin: null` parses to an empty netloc and so never matches.
            return urlsplit(value).netloc.lower() == host.lower()
    return False


async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    if is_ui_path(request.url.path):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        # `same-origin`, not `no-referrer`: with no-referrer a browser sends
        # `Origin: null` on a same-origin POST and the check above would refuse it.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
    return response
