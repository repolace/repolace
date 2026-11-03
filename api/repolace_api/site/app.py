"""The public site: landing, pricing, benchmark snapshot, legal pages and the waitlist form.

A separate ASGI app from `repolace_api.main`, run as its own process on its own port. It mounts no
router from `repolace_api.routes` and not `repolace_api.ui`, so `/ui`, `/repos`, `/tasks` and
`/github/webhook` do not exist here (they answer 404), whatever `Host` a request carries. The
Cloudflare Tunnel points at this port only; the API and the task UI stay on loopback behind
Tailscale (see `.claude/rules/api-and-ui.md`).
"""

import hashlib
import ipaddress
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from repolace_api.config import SiteSettings
from repolace_api.site import content
from repolace_api.site.security import (
    SiteSecurityMiddleware,
    content_security_policy,
    normalise_host,
    parse_allowed_hosts,
    same_origin,
    security_headers,
)
from repolace_api.site.waitlist import (
    MAX_BODY_BYTES,
    SlidingWindowLimiter,
    SqlWaitlistStore,
    TurnstileVerifier,
    WaitlistStore,
    normalise_email,
)

HERE = Path(__file__).parent

_MESSAGES = {
    "email": "That does not look like an email address.",
    "consent": "Please tick the box so we know you want to hear from us.",
    "limit": "Too many attempts from this address. Try again in a while.",
    "captcha": "The human check did not pass. Reload the page and try again.",
    "unavailable": "The waitlist is not accepting signups right now. Please try again later.",
    "body": "That request was too large.",
}


def _asset_version(directory: Path) -> str:
    """A short hash of the site's own CSS and JS, appended to their URLs as `?v=`.

    Cloudflare rewrites `Cache-Control` on static files to `max-age=14400`, so a returning visitor's browser
    can keep a previous deploy's stylesheet for hours; new HTML on old CSS looks broken. A changed file gets a
    new URL, so it is fetched at once, and an unchanged one stays cached.
    """
    digest = hashlib.sha256()
    for name in ("site.css", "site.js", "theme-init.js", "favicon.svg", "favicon.png", "apple-touch-icon.png"):
        digest.update((directory / name).read_bytes())
    return digest.hexdigest()[:10]


def _is_local_peer(peer: str) -> bool:
    """True for a loopback or private-range peer: the tunnel connector, directly or over a Docker bridge.

    The site port is published on 127.0.0.1 only, so nothing else can be the TCP peer; a client that
    reaches it from elsewhere cannot spoof `CF-Connecting-IP`.
    """
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


class StaticAssets(StaticFiles):
    """Everything revalidates (`no-cache`); `immutable_suffix` opts one file type into a year-long cache."""

    def __init__(self, *, immutable_suffix: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._immutable_suffix = immutable_suffix

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            immutable = self._immutable_suffix is not None and path.endswith(self._immutable_suffix)
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return response


def create_site_app(
    settings: SiteSettings | None = None,
    store: WaitlistStore | None = None,
    turnstile: TurnstileVerifier | None = None,
    limiter: SlidingWindowLimiter | None = None,
) -> FastAPI:
    settings = settings or SiteSettings()
    hosts = parse_allowed_hosts(settings.site_allowed_hosts)
    turnstile_on = bool(settings.turnstile_site_key and settings.turnstile_secret_key)
    if turnstile is None and turnstile_on:
        turnstile = TurnstileVerifier(settings.turnstile_secret_key or "", hosts)

    engine = None
    if store is None and settings.site_database_url:
        from repolace_shared.db.session import create_engine, create_session_factory

        engine = create_engine(settings.site_database_url)
        store = SqlWaitlistStore(create_session_factory(engine))
    #: Signups per address per hour, and a ceiling on the whole site so one botnet cannot fill the table.
    per_ip = limiter or SlidingWindowLimiter(limit=5, window=3600)
    overall = SlidingWindowLimiter(limit=300, window=3600)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="repolace site", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=HERE / "templates")
    asset_v = _asset_version(HERE / "static")

    def page(request: Request, name: str, status: int = 200, **ctx) -> Response:
        base = {
            "turnstile_site_key": settings.turnstile_site_key if turnstile_on else None,
            "plans": content.PLANS,
            "overage": content.OVERAGE_USD,
            "facts": content.load_facts(),
            "path": request.url.path,
            "asset_v": asset_v,
        }
        return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status)

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else ""
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        if settings.site_trust_cf_ip and forwarded and _is_local_peer(peer):
            return forwarded[:64]
        return peer or "unknown"

    @app.get("/", include_in_schema=False)
    async def landing(request: Request) -> Response:
        return page(request, "landing.html")

    @app.get("/pricing", include_in_schema=False)
    async def pricing(request: Request) -> Response:
        return page(request, "pricing.html")

    @app.get("/privacy", include_in_schema=False)
    async def privacy(request: Request) -> Response:
        return page(request, "privacy.html")

    @app.get("/terms", include_in_schema=False)
    async def terms(request: Request) -> Response:
        return page(request, "terms.html")

    @app.get("/waitlist", include_in_schema=False)
    async def waitlist_form(request: Request) -> Response:
        return page(request, "waitlist.html")

    @app.get("/waitlist/thanks", include_in_schema=False)
    async def waitlist_thanks(request: Request) -> Response:
        return page(request, "waitlist_thanks.html")

    @app.post("/waitlist", include_in_schema=False)
    async def waitlist_submit(request: Request) -> Response:
        def fail(code: str, status: int) -> Response:
            return page(request, "waitlist.html", status=status, error=_MESSAGES[code])

        if not same_origin(request, hosts) or normalise_host(request.headers.get("host", "")) not in hosts:
            return PlainTextResponse("Forbidden", status_code=403)
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return fail("body", 413)
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY_BYTES:
                return fail("body", 413)
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}

        ip = client_ip(request)
        if not per_ip.allow(ip) or not overall.allow("*"):
            return fail("limit", 429)
        if form.get("website"):  # honeypot: a human never sees this field
            return RedirectResponse("/waitlist/thanks", status_code=303)
        if turnstile is not None and not await turnstile.verify(form.get("cf-turnstile-response", ""), ip):
            return fail("captcha", 400)
        email = normalise_email(form.get("email", ""))
        if email is None:
            return fail("email", 400)
        if form.get("consent") != "yes":
            return fail("consent", 400)
        if store is None:
            return fail("unavailable", 503)
        await store.add(email)  # a database error is a 500 page; uvicorn logs the traceback
        return RedirectResponse("/waitlist/thanks", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico() -> FileResponse:
        # Browsers ask for this path when a page has no icon link (feeds, direct image loads); a PNG is accepted.
        return FileResponse(HERE / "static" / "favicon.png", media_type="image/png")

    @app.get("/robots.txt", include_in_schema=False)
    async def robots() -> PlainTextResponse:
        return PlainTextResponse("User-agent: *\nAllow: /\n")

    # The benchmark report: a self-contained static site (a copy of BENCHMARK/site from the
    # feat/benchmark-site branch, see benchmark_site/README.md). `/benchmark` redirects to `/benchmark/`
    # so its relative URLs resolve.
    app.mount("/benchmark", StaticAssets(directory=HERE / "benchmark_site", html=True), name="benchmark")
    app.mount("/assets", StaticAssets(directory=HERE / "static"), name="assets")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        status = exc.status_code
        message = "Page not found." if status == 404 else "That request could not be handled."
        return page(request, "error.html", status=status, code=status, message=message)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        # Runs outside the middleware, so the security headers are set here as well.
        response = page(request, "error.html", status=500, code=500, message="Something went wrong on our side.")
        response.headers.update(app.state.security_headers)
        return response

    headers = security_headers(content_security_policy(turnstile=turnstile_on))
    app.state.security_headers = headers
    app.add_middleware(SiteSecurityMiddleware, hosts=hosts, headers=headers)
    return app
