"""An httpx transport that retries the failures a flaky connection causes.

The first benchmark runs were made over a phone hotspot, and two tasks in an hour died to a dropped
connection: once in `git clone`, once in the very first GitHub call (`github unreachable: ` with an empty
message). A task that fails like that is a harness error, never re-queued, and counts against the headline.

What is retried, and why that is safe:

* `ConnectError` and `ConnectTimeout`, for **any** method: the request never reached the server, so
  repeating it cannot do anything twice (including opening a second pull request).
* `ReadTimeout`, `ReadError`, `RemoteProtocolError` and a 502, 503 or 504 response, for **GET and HEAD
  only**: those are idempotent, and a response lost after the server acted is harmless to ask for again.

A POST that fails after the connection was made is not retried: the pull request may exist.
"""

import asyncio
import urllib.request
from collections.abc import Sequence

import httpx
import structlog

log = structlog.get_logger()

ATTEMPTS = 3
#: Set to True in a request's `extensions` to say repeating it is harmless (a token mint).
RETRY_EXTENSION = "repolace_retry"
DELAYS_SECONDS: Sequence[float] = (0.5, 2.0)

_NEVER_REACHED_SERVER = (httpx.ConnectError, httpx.ConnectTimeout)
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})
_IDEMPOTENT_ERRORS = (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)
_TRANSIENT_STATUSES = frozenset({502, 503, 504})


def _environment_proxy() -> str | None:
    """The proxy the environment names for https, which httpx stops applying once a transport is passed in.

    Git is given the same variables, so without this the API calls would go direct while git used the proxy.
    """
    if urllib.request.proxy_bypass("api.github.com"):
        return None
    return urllib.request.getproxies().get("https")


class RetryingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(proxy=_environment_proxy())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        idempotent = request.method in _IDEMPOTENT_METHODS or bool(request.extensions.get(RETRY_EXTENSION))
        for attempt in range(1, ATTEMPTS + 1):
            last = attempt == ATTEMPTS
            try:
                response = await self._inner.handle_async_request(request)
            except _NEVER_REACHED_SERVER as exc:
                if last:
                    raise
                self._note(request, attempt, type(exc).__name__)
            except _IDEMPOTENT_ERRORS as exc:
                if last or not idempotent:
                    raise
                self._note(request, attempt, type(exc).__name__)
            else:
                if response.status_code in _TRANSIENT_STATUSES and idempotent and not last:
                    await response.aclose()
                    self._note(request, attempt, f"HTTP {response.status_code}")
                else:
                    return response
            await asyncio.sleep(DELAYS_SECONDS[min(attempt - 1, len(DELAYS_SECONDS) - 1)])
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover

    @staticmethod
    def _note(request: httpx.Request, attempt: int, reason: str) -> None:
        log.warning("github.http.retry", method=request.method, host=request.url.host, attempt=attempt, reason=reason)

    async def aclose(self) -> None:
        await self._inner.aclose()
