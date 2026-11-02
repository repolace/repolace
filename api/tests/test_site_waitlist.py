"""The waitlist form: validation, abuse limits, the honeypot, Turnstile, and idempotent storage."""

import httpx
import pytest
from repolace_api.config import SiteSettings
from repolace_api.site import create_site_app
from repolace_api.site.waitlist import SlidingWindowLimiter, TurnstileVerifier, normalise_email

HOST = "repolace.example.test"
ORIGIN = {"Origin": f"https://{HOST}"}

pytestmark = pytest.mark.anyio


class MemoryStore:
    def __init__(self):
        self.emails: list[str] = []

    async def add(self, email: str) -> None:
        if email not in self.emails:
            self.emails.append(email)


class FakeTurnstile:
    def __init__(self, ok: bool):
        self.ok = ok
        self.seen: list[tuple[str, str | None]] = []

    async def verify(self, token, remote_ip):
        self.seen.append((token, remote_ip))
        return self.ok


def make(store=None, turnstile=None, limiter=None, **settings):
    cfg = SiteSettings(site_allowed_hosts=HOST, _env_file=None, **settings)
    app = create_site_app(cfg, store=store, turnstile=turnstile, limiter=limiter)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}")
    return client


GOOD = {"email": "Ada@Example.com", "consent": "yes"}


async def post(client, data, headers=ORIGIN):
    return await client.post("/waitlist", data=data, headers=headers)


@pytest.mark.parametrize("raw, want", [("Ada@Example.com ", "ada@example.com"), ("a@b.co", "a@b.co"), ("nope", None), ("a@b", None), ("a b@c.de", None), ("", None), ("x" * 250 + "@a.bc", None)])
def test_normalise_email(raw, want):
    assert normalise_email(raw) == want


async def test_signup_stores_lowercased_address_and_redirects():
    store = MemoryStore()
    async with make(store) as client:
        resp = await post(client, GOOD)
    assert resp.status_code == 303 and resp.headers["location"] == "/waitlist/thanks"
    assert store.emails == ["ada@example.com"]


async def test_repeat_signup_looks_identical_and_stores_once():
    store = MemoryStore()
    async with make(store) as client:
        first = await post(client, GOOD)
        second = await post(client, GOOD)
    assert (first.status_code, first.headers["location"]) == (second.status_code, second.headers["location"])
    assert store.emails == ["ada@example.com"]


@pytest.mark.parametrize("data, status", [({"email": "nope", "consent": "yes"}, 400), ({"email": "a@b.co"}, 400)])
async def test_invalid_input_is_rejected_with_a_message(data, status):
    store = MemoryStore()
    async with make(store) as client:
        resp = await post(client, data)
    assert resp.status_code == status and 'role="alert"' in resp.text
    assert store.emails == []


async def test_honeypot_fills_nothing_but_looks_like_success():
    store = MemoryStore()
    async with make(store) as client:
        resp = await post(client, {**GOOD, "website": "http://spam"})
    assert resp.status_code == 303
    assert store.emails == []


async def test_cross_site_and_missing_origin_are_refused():
    store = MemoryStore()
    async with make(store) as client:
        for headers in ({"Origin": "https://evil.example"}, {}, {"Origin": "null"}, {**ORIGIN, "Sec-Fetch-Site": "cross-site"}):
            assert (await post(client, GOOD, headers)).status_code == 403
    assert store.emails == []


async def test_oversized_body_is_refused():
    store = MemoryStore()
    async with make(store) as client:
        resp = await post(client, {**GOOD, "pad": "x" * 20_000})
    assert resp.status_code == 413
    assert store.emails == []


async def test_per_address_rate_limit():
    store = MemoryStore()
    limiter = SlidingWindowLimiter(limit=2, window=60)
    async with make(store, limiter=limiter) as client:
        codes = [(await post(client, {**GOOD, "email": f"u{i}@example.com"})).status_code for i in range(3)]
    assert codes == [303, 303, 429]


async def test_no_database_configured_is_a_clear_503_not_a_crash():
    async with make(store=None) as client:
        resp = await post(client, GOOD)
    assert resp.status_code == 503


async def test_turnstile_failure_blocks_and_success_passes_the_token_through():
    store = MemoryStore()
    bad, good = FakeTurnstile(False), FakeTurnstile(True)
    keys = {"turnstile_site_key": "site", "turnstile_secret_key": "secret"}
    async with make(store, turnstile=bad, **keys) as client:
        assert (await post(client, {**GOOD, "cf-turnstile-response": "tok"})).status_code == 400
    assert store.emails == []
    async with make(store, turnstile=good, **keys) as client:
        assert (await post(client, {**GOOD, "cf-turnstile-response": "tok"})).status_code == 303
    assert good.seen[0][0] == "tok"


async def test_forwarded_ip_is_trusted_only_from_loopback_peers():
    # The ASGI test transport reports the peer as 127.0.0.1, so CF-Connecting-IP is honoured.
    limiter = SlidingWindowLimiter(limit=1, window=60)
    async with make(MemoryStore(), limiter=limiter) as client:
        a = await post(client, GOOD, {**ORIGIN, "CF-Connecting-IP": "203.0.113.1"})
        b = await post(client, {**GOOD, "email": "b@example.com"}, {**ORIGIN, "CF-Connecting-IP": "203.0.113.2"})
        c = await post(client, {**GOOD, "email": "c@example.com"}, {**ORIGIN, "CF-Connecting-IP": "203.0.113.1"})
    assert (a.status_code, b.status_code, c.status_code) == (303, 303, 429)


def test_limiter_window_expires_and_memory_is_bounded():
    now = [0.0]
    lim = SlidingWindowLimiter(limit=1, window=10, max_keys=3, clock=lambda: now[0])
    assert lim.allow("a") and not lim.allow("a")
    now[0] = 11
    assert lim.allow("a")
    for k in "bcdef":
        lim.allow(k)
    assert len(lim._events) <= 3


async def test_turnstile_verifier_checks_success_and_hostname_and_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "secret=s" in body and "response=tok" in body
        return httpx.Response(200, json={"success": True, "hostname": HOST})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = TurnstileVerifier("s", frozenset({HOST}), client)
        assert await ok.verify("tok", "1.2.3.4") is True
        assert await TurnstileVerifier("s", frozenset({"other"}), client).verify("tok", None) is False
        assert await ok.verify("", None) is False
        assert await ok.verify("x" * 2049, None) is False

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        assert await TurnstileVerifier("s", frozenset({HOST}), client).verify("tok", None) is False
