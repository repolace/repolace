from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from repolace_api.config import get_settings
from repolace_api.routes.github import router as github_router
from repolace_api.routes.repos import router as repos_router
from repolace_shared.db.session import create_engine, create_session_factory
from repolace_shared.github.client import GithubClient
from repolace_shared.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging("api")
    settings = get_settings()

    engine = create_engine(settings.database_url)
    app.state.session_factory = create_session_factory(engine)
    app.state.github_client = GithubClient(settings.github_app_id, settings.github_app_private_key)

    yield

    await app.state.github_client.aclose()
    await engine.dispose()


app = FastAPI(title="repolace", lifespan=lifespan)
app.include_router(github_router)
app.include_router(repos_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
