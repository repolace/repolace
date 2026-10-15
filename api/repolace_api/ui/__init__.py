"""The web UI: server-rendered Jinja2 + HTMX + plain CSS, served same-origin by the API app.

`mount_ui(app)` is the one entry point, so `main.py` and the tests wire the
pages, the static files, the headers and the HTML error pages identically.
"""

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from repolace_api.ui.router import error_page, redirect_root, router
from repolace_api.ui.security import is_ui_path, security_headers

STATIC_DIR = Path(__file__).parent / "static"


class UiStaticFiles(StaticFiles):
    """Fonts never change under one URL, so they are cached forever; the rest revalidates."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            immutable = path.startswith("fonts/") and path.endswith(".woff2")
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return response


def mount_ui(app: FastAPI) -> None:
    app.include_router(router)
    app.mount("/ui/static", UiStaticFiles(directory=STATIC_DIR), name="ui-static")
    app.middleware("http")(security_headers)
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


__all__ = ["STATIC_DIR", "mount_ui"]
