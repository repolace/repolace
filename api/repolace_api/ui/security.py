"""Response headers and the same-origin check for the HTML UI.

There is no authentication until Phase 4 (RBAC), so the UI is for localhost or
a private tunnel and nothing more. What this module adds is narrow:

* a Content-Security-Policy that forbids inline script, inline style, remote
  assets and framing, so an escaping mistake in a template has less to work with;
* a `Host` allowlist (`UI_ALLOWED_HOSTS`, default localhost, 127.0.0.1, [::1]) on
  every request, so a DNS-rebinding page, whose requests carry the attacker's
  `Host`, cannot read pages or the JSON API;
* a same-origin check on the one state-changing HTML request (enqueue): the
  `Origin` (else `Referer`) host must be on that allowlist. A browser always
  sends `Origin` on a cross-origin POST; a request with neither is refused too
  (curl and scripts should use the JSON API, which this does not guard).

Limit, stated plainly: this stops cross-site request forgery. It does not stop
anyone who can reach the port from enqueueing a task with a hand-built request,
because they can send whatever `Origin` they like.
"""

from urllib.parse import urlsplit

from fastapi import Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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

#: `Referrer-Policy: same-origin`, not `no-referrer`: with no-referrer a browser sends
#: `Origin: null` on a same-origin POST and `same_origin` would refuse it.
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}

UI_PREFIX = "/ui"


def is_ui_path(path: str) -> bool:
    return path == UI_PREFIX or path.startswith(UI_PREFIX + "/")


DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def normalise_host(value: str) -> str:
    """`Host`-style value to a bare lowercase host: no port, no IPv6 brackets."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1 : value.index("]")] if "]" in value else ""
    return value.split(":", 1)[0]


def parse_allowed_hosts(raw: str | list[str] | tuple[str, ...] | None) -> frozenset[str]:
    """A comma-separated setting (or a sequence) to the normalised allowlist; empty falls back to localhost."""
    items = raw.split(",") if isinstance(raw, str) else list(raw or ())
    hosts = {normalise_host(i) for i in items if i.strip()}
    return frozenset(hosts - {""}) or frozenset(normalise_host(h) for h in DEFAULT_ALLOWED_HOSTS)


def allowed_hosts(request_or_app: Request | ASGIApp) -> frozenset[str]:
    app = request_or_app.app if isinstance(request_or_app, Request) else request_or_app
    return app.state.ui_allowed_hosts


def same_origin(request: Request) -> bool:
    """True when the request's `Origin` (else `Referer`) host is on the allowlist.

    The comparison is against the configured allowlist, never against `Host`:
    under DNS rebinding the attacker controls `Host` (and so an `Origin` that
    matches it), but not this list.
    """
    if normalise_host(request.headers.get("host", "")) not in allowed_hosts(request):
        return False
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return False
    for name in ("origin", "referer"):
        value = request.headers.get(name)
        if value is not None:
            try:
                host = urlsplit(value).hostname  # `Origin: null` has none; `http://[` raises
            except ValueError:
                return False
            return host is not None and host.lower() in allowed_hosts(request)
    return False


class UiSecurityMiddleware:
    """Pure ASGI: refuses a `Host` off the allowlist (DNS rebinding) and stamps the UI's headers.

    The headers go on in the send wrapper, so every response this app produces
    carries them, including an HTML 404 or a handled 500. A response from
    Starlette's outermost error layer bypasses it; `mount_ui` registers an
    `Exception` handler for that case.
    """

    def __init__(self, app: ASGIApp, hosts: frozenset[str]):
        self.app = app
        self.hosts = hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if normalise_host(headers.get("host", "")) not in self.hosts:
            response = PlainTextResponse("Invalid host header", status_code=400)
            await response(scope, receive, send)
            return
        if not is_ui_path(scope["path"]):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(SECURITY_HEADERS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


