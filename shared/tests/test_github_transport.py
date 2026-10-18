"""The GitHub client's transport retries what a flaky connection causes, and nothing that could happen twice."""

import httpx
import pytest
from repolace_shared.github import transport
from repolace_shared.github.transport import RetryingTransport

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    monkeypatch.setattr(transport, "DELAYS_SECONDS", (0.0, 0.0))


class Scripted(httpx.AsyncBaseTransport):
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, request=request)


async def send(inner, method="GET"):
    async with httpx.AsyncClient(transport=RetryingTransport(inner)) as client:
        return await client.request(method, "https://api.github.com/x")


async def test_a_connect_error_is_retried():
    inner = Scripted(httpx.ConnectError(""), httpx.ConnectError(""), 200)
    assert (await send(inner)).status_code == 200
    assert inner.calls == 3


async def test_a_connect_error_is_retried_even_for_post():
    inner = Scripted(httpx.ConnectError(""), 201)
    assert (await send(inner, "POST")).status_code == 201


async def test_it_gives_up_after_three_attempts():
    inner = Scripted(httpx.ConnectError("down"))
    with pytest.raises(httpx.ConnectError):
        await send(inner)
    assert inner.calls == 3


async def test_a_read_failure_is_retried_for_get_only():
    inner = Scripted(httpx.ReadTimeout(""), 200)
    assert (await send(inner)).status_code == 200

    post = Scripted(httpx.ReadTimeout(""), 201)
    with pytest.raises(httpx.ReadTimeout):
        await send(post, "POST")
    assert post.calls == 1


async def test_a_bad_gateway_is_retried_for_get_but_not_post():
    inner = Scripted(502, 200)
    assert (await send(inner)).status_code == 200

    post = Scripted(502, 201)
    assert (await send(post, "POST")).status_code == 502
    assert post.calls == 1


async def test_a_client_error_is_returned_at_once():
    inner = Scripted(404, 200)
    assert (await send(inner)).status_code == 404
    assert inner.calls == 1
