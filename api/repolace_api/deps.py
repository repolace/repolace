from collections.abc import AsyncIterator

from fastapi import Request
from repolace_shared.github.client import GithubClient
from sqlalchemy.ext.asyncio import AsyncSession


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def get_github_client(request: Request) -> GithubClient:
    return request.app.state.github_client
