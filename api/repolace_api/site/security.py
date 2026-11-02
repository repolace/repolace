"""Host allowlist, response headers and the same-origin check for the public site.

This is the only process the internet reaches, so it is a separate ASGI app with its own copy of
these helpers: it must not import `repolace_api.ui` (whose package `__init__` pulls in the task and
repository routes) or any router that can enqueue work. The checks are deliberately the same shape
as `repolace_api/ui/security.py`; a change to one should be looked at against the other.
"""

from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]")
TURNSTILE_ORIGIN = "https://challenges.cloudflare.com"

#: Paths that skip the Host allowlist: the tunnel connector's health probe carries no `Host` we control.
HOST_CHECK_EXEMPT_PATHS = frozenset({"/healthz"})


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


def content_security_policy(*, turnstile: bool) -> str:
    """No inline script or style, no remote origin, no framing. Turnstile adds its one origin."""
    script = "script-src 'self'" + (f" {TURNSTILE_ORIGIN}" if turnstile else "")
    frame = f"frame-src {TURNSTILE_ORIGIN}" if turnstile else "frame-src 'none'"
    connect = "connect-src 'self'" + (f" {TURNSTILE_ORIGIN}" if turnstile else "")
    return "; ".join(
        [
            "default-src 'self'",
            script,
            "style-src 'self'",
            "font-src 'self'",
            "img-src 'self'",
            connect,
            frame,
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


def security_headers(csp: str) -> dict[str, str]:
    return {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }


def same_origin(request: Request, hosts: frozenset[str]) -> bool:
    """True when the request's `Origin` (else `Referer`) host is on the allowlist and the fetch site is ours.

    Compared with the configured list, never with `Host`, which the sender controls.
    """
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
            return host is not None and host.lower() in hosts
    return False


class SiteSecurityMiddleware:
    """Pure ASGI: refuses a `Host` off the allowlist and stamps every response with the site's headers."""

    def __init__(self, app: ASGIApp, hosts: frozenset[str], headers: dict[str, str]):
        self.app = app
        self.hosts = hosts
        self.headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = normalise_host(Headers(scope=scope).get("host", ""))
        if scope["path"] not in HOST_CHECK_EXEMPT_PATHS and host not in self.hosts:
            await PlainTextResponse("Invalid host header", status_code=400)(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(self.headers)
            await send(message)

        await self.app(scope, receive, send_with_headers)
