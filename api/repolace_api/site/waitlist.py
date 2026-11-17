"""Waitlist: input checks, the per-address store and the Turnstile check. The limiter lives in
`repolace_api.ratelimit`, shared with the API."""

import re
from typing import Protocol

import httpx
from repolace_shared.db.models import WaitlistSignup
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: One "@", something on both sides, a dot in the domain, no whitespace. Not RFC 5322: a bounce is
#: the real validation, and a stricter regex only rejects real addresses.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254
MAX_BODY_BYTES = 8 * 1024


def normalise_email(raw: str) -> str | None:
    """The lower-cased address, or None when it is not plausibly one."""
    email = raw.strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH or not _EMAIL.fullmatch(email):
        return None
    return email


class WaitlistStore(Protocol):
    async def add(self, email: str) -> None:
        """Record `email`. Adding an address already present is a no-op, never an error."""


class SqlWaitlistStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def add(self, email: str) -> None:
        # No conflict target on purpose: naming `(email)` makes Postgres require SELECT on that column,
        # and the site's database role may INSERT into this table and nothing else.
        stmt = insert(WaitlistSignup).values(email=email, consent=True, source="site").on_conflict_do_nothing()
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()


class TurnstileVerifier:
    """Server-side check of a Turnstile token (POST to siteverify). Fails closed on any error."""

    URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    def __init__(self, secret: str, expected_hosts: frozenset[str], client: httpx.AsyncClient | None = None):
        self._secret = secret
        self._expected_hosts = expected_hosts
        self._client = client

    async def verify(self, token: str, remote_ip: str | None) -> bool:
        if not token or len(token) > 2048:
            return False
        data = {"secret": self._secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        try:
            if self._client is not None:
                resp = await self._client.post(self.URL, data=data, timeout=10)
            else:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(self.URL, data=data, timeout=10)
            resp.raise_for_status()
            result = resp.json()
        except (httpx.HTTPError, ValueError):
            return False
        hostname = str(result.get("hostname", "")).lower()
        return result.get("success") is True and hostname in self._expected_hosts
