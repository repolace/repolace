import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from repolace_shared.db.session import create_engine, create_session_factory
from repolace_shared.github.client import GithubClient
from repolace_shared.logging import configure_logging

from repolace_api.config import UiSettings, access_startup_error, get_billing_settings, get_settings
from repolace_api.publisher import CeleryTaskPublisher
from repolace_api.routes.auth import router as auth_router
from repolace_api.routes.billing import router as billing_router
from repolace_api.routes.github import router as github_router
from repolace_api.routes.repos import router as repos_router
from repolace_api.routes.tasks import router as tasks_router
from repolace_api.ui import mount_ui
from repolace_api.ui.security import parse_allowed_hosts


class RedactOAuthCallback(logging.Filter):
    """Keep the GitHub sign-in code and state out of uvicorn's access log.

    The access line of `GET /auth/github/callback?code=...&state=...` would otherwise carry both. The
    code is single-use and needs the client secret, but the sign-in module promises it is never logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and args[2].startswith("/auth/github/callback?"):
            record.args = (*args[:2], "/auth/github/callback?[redacted]", *args[3:])
        return True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging("api")
    logging.getLogger("uvicorn.access").addFilter(RedactOAuthCallback())
    settings = get_settings()
    problem = access_startup_error(get_billing_settings(), sorted(parse_allowed_hosts(UiSettings().ui_allowed_hosts)))
    if problem is not None:
        raise RuntimeError(f"refusing to start: {problem}")

    engine = create_engine(settings.database_url)
    app.state.session_factory = create_session_factory(engine)
    app.state.github_client = GithubClient(settings.github_app_id, settings.github_app_private_key)

    app.state.publisher = CeleryTaskPublisher(settings.celery_broker_url)

    yield

    app.state.publisher.close()

    await app.state.github_client.aclose()
    await engine.dispose()


app = FastAPI(title="repolace", lifespan=lifespan)
app.include_router(github_router)
app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(repos_router)
app.include_router(tasks_router)
mount_ui(app)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
