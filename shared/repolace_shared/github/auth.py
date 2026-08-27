import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import jwt

GITHUB_API_BASE = "https://api.github.com"

#: Default freshness margin: enough for one API call to complete.
DEFAULT_MIN_TTL_SECONDS = 60.0


def build_app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": app_id}
    return jwt.encode(payload, private_key, algorithm="RS256")


@dataclass
class _CachedToken:
    token: str
    expires_at: float


class InstallationTokenCache:
    def __init__(self, app_id: str, private_key: str) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._tokens: dict[int, _CachedToken] = {}

    async def get_token(
        self,
        installation_id: int,
        client: httpx.AsyncClient,
        min_ttl_seconds: float = DEFAULT_MIN_TTL_SECONDS,
    ) -> str:
        """Return a token guaranteed to outlive ``min_ttl_seconds``.

        The margin is a parameter because callers need different ones. A single
        API call is fine with seconds; a `git push` is a network round-trip
        that can stall, and one that expires mid-transfer fails the last step
        of a task that has already done all of its work.
        """
        cached = self._tokens.get(installation_id)
        if cached and cached.expires_at - time.time() > min_ttl_seconds:
            return cached.token

        app_jwt = build_app_jwt(self._app_id, self._private_key)
        response = await client.post(
            f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        data = response.json()
        expires_at = datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
        cached = _CachedToken(token=data["token"], expires_at=expires_at)
        self._tokens[installation_id] = cached
        return cached.token
