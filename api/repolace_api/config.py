import base64
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class ApiSettings(BaseSettings):
    # In compose the `api` container loads infra/api.env, not the repository .env, so its
    # environment holds only what is read here and in `UiSettings`. Add a field here and add it to
    # infra/api.env.example too; `api/tests/test_api_env_example.py` holds the two equal.
    model_config = SettingsConfigDict(env_file=_REPO_ROOT_ENV_FILE, extra="ignore")

    database_url: str
    redis_url: str
    celery_broker_url: str

    github_app_id: str
    github_app_slug: str
    github_app_private_key_base64: str
    github_app_webhook_secret: str
    api_base_url: str = "http://localhost:8000"

    @property
    def github_app_private_key(self) -> str:
        return base64.b64decode(self.github_app_private_key_base64).decode("utf-8")


@lru_cache
def get_settings() -> ApiSettings:
    return ApiSettings()


class UiSettings(BaseSettings):
    """Only what `mount_ui` needs, so building the app never requires the database or GitHub values."""

    model_config = SettingsConfigDict(env_file=_REPO_ROOT_ENV_FILE, extra="ignore")

    #: Comma-separated `Host` values the UI and API answer to (DNS-rebinding guard).
    #: Add a tunnel hostname here; `UI_ALLOWED_HOSTS=localhost,127.0.0.1,[::1],my.tunnel.example`.
    ui_allowed_hosts: str = "localhost,127.0.0.1,[::1]"


class SiteSettings(BaseSettings):
    """The public site process (`repolace_api.site`). Deliberately its own class: that process must
    never need the GitHub App, the broker or the UI allowlist, and the API must never learn the
    site's secrets. Listed in infra/site.env.example."""

    model_config = SettingsConfigDict(env_file=_REPO_ROOT_ENV_FILE, extra="ignore")

    #: `Host` values the public site answers to; anything else is a 400.
    site_allowed_hosts: str = "localhost,127.0.0.1,[::1]"
    #: A role that can INSERT into `waitlist_signups` and nothing else. Unset: the form answers 503.
    site_database_url: str | None = None
    #: Cloudflare Turnstile. Both set turns the check on (and the widget); neither set leaves only
    #: the honeypot and the rate limit.
    turnstile_site_key: str | None = None
    turnstile_secret_key: str | None = None
    #: Trust `CF-Connecting-IP` when the TCP peer is loopback or private (the tunnel connector).
    site_trust_cf_ip: bool = True
