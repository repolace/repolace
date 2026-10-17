"""The web UI: server-rendered Jinja2 + HTMX + plain CSS, served same-origin by the API app.

`mount_ui(app)` is the one entry point, so `main.py` and the tests wire the
pages, the static files, the headers and the HTML error pages identically.
"""

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse
from starlette.staticfiles import StaticFiles

from repolace_api.config import UiSettings
from repolace_api.ui.router import error_page, redirect_root, router
from repolace_api.ui.security import SECURITY_HEADERS, UiSecurityMiddleware, is_ui_path, parse_allowed_hosts

STATIC_DIR = Path(__file__).parent / "static"


class UiStaticFiles(StaticFiles):
    """Fonts never change under one URL, so they are cached forever; the rest revalidates."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            immutable = path.startswith("fonts/") and path.endswith(".woff2")
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return response


def mount_ui(app: FastAPI, allowed_hosts: str | list[str] | None = None) -> None:
    """Wire the pages, static files, host allowlist, headers and HTML error pages into `app`.

    `allowed_hosts` defaults to the `UI_ALLOWED_HOSTS` setting (localhost only if unset).
    """
    hosts = parse_allowed_hosts(allowed_hosts if allowed_hosts is not None else UiSettings().ui_allowed_hosts)
    app.state.ui_allowed_hosts = hosts
    app.include_router(router)
    app.mount("/ui/static", UiStaticFiles(directory=STATIC_DIR), name="ui-static")
    app.add_middleware(UiSecurityMiddleware, hosts=hosts)
    app.add_api_route("/", redirect_root, include_in_schema=False)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if is_ui_path(request.url.path):
            return error_page(exc.status_code, str(exc.detail))
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        if is_ui_path(request.url.path):
            return error_page(404, "Not found.")
        return await request_validation_exception_handler(request, exc)


    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        # Runs in Starlette's outermost layer, outside the middleware, so the headers are set here.
        # The exception itself is logged by uvicorn; nothing from it reaches the page.
        if is_ui_path(request.url.path):
            response = error_page(500, "Something went wrong. See the server log.")
            response.headers.update(SECURITY_HEADERS)
            return response
        return PlainTextResponse("Internal Server Error", status_code=500)


__all__ = ["STATIC_DIR", "mount_ui"]
