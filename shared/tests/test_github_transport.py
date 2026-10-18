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


async def test_connect_timeout_is_retried_for_post():
    inner = Scripted(httpx.ConnectTimeout(""), 201)
    assert (await send(inner, "POST")).status_code == 201


@pytest.mark.parametrize("error", [httpx.RemoteProtocolError(""), httpx.ReadError("")])
async def test_other_read_failures_are_retried_for_get(error):
    inner = Scripted(error, 200)
    assert (await send(inner)).status_code == 200


@pytest.mark.parametrize("status", [503, 504])
async def test_other_gateway_statuses_are_retried_for_get(status):
    assert (await send(Scripted(status, 200))).status_code == 200


async def test_the_last_attempt_returns_a_bad_gateway_instead_of_raising():
    inner = Scripted(502)
    assert (await send(inner)).status_code == 502
    assert inner.calls == 3


async def test_a_request_can_opt_in_to_retrying_a_post():
    inner = Scripted(httpx.ReadTimeout(""), 201)
    async with httpx.AsyncClient(transport=RetryingTransport(inner)) as client:
        response = await client.post("https://api.github.com/x", extensions={transport.RETRY_EXTENSION: True})
    assert response.status_code == 201
    assert inner.calls == 2


async def test_cancellation_during_the_pause_propagates(monkeypatch):
    import asyncio

    async def cancelled(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(transport.asyncio, "sleep", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await send(Scripted(httpx.ConnectError(""), 200))


async def test_a_502_response_is_closed_before_the_retry():
    closed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b""

        async def aclose(self):
            closed.append(True)

    class Inner(Scripted):
        async def handle_async_request(self, request):
            self.calls += 1
            if self.calls == 1:
                return httpx.Response(502, stream=Body(), request=request)
            return httpx.Response(200, request=request)

    assert (await send(Inner(200))).status_code == 200
    assert closed == [True]


def test_the_environment_proxy_is_still_used(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    assert transport._environment_proxy() == "http://proxy.example:3128"
    monkeypatch.delenv("HTTPS_PROXY")
    monkeypatch.delenv("https_proxy", raising=False)
    assert transport._environment_proxy() is None


async def test_the_client_goes_through_the_retrying_transport():
    from repolace_shared.github.client import GithubClient

    inner = Scripted(httpx.ConnectError(""), 200)
    client = GithubClient("1", "unused", transport=inner)
    assert (await client._http.get("/x")).status_code == 200
    assert inner.calls == 2
    await client._http.aclose()
