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


class BillingSettings(BaseSettings):
    """Stripe billing and GitHub sign-in. Every field is optional: unset, the billing and sign-in
    routes answer 503 and the rest of the API is unaffected. Listed in infra/api.env.example.

    Use a restricted key (`rk_`) with the minimum permissions: Checkout Sessions, Customers,
    Customer Portal sessions and Subscriptions (read). Never a live key from a laptop.
    """

    model_config = SettingsConfigDict(env_file=_REPO_ROOT_ENV_FILE, extra="ignore")

    stripe_secret_key: str | None = None
    #: The signing secret of the webhook endpoint (`whsec_...`; `stripe listen` prints one locally).
    stripe_webhook_secret: str | None = None
    #: The Price id (`price_...`) of the paid monthly plan.
    stripe_price_id: str | None = None
    #: Signs the session cookie. At least 32 random characters.
    session_secret: str | None = None
    #: The GitHub App's OAuth client id and secret ("Request user authorization").
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    #: Where Stripe and GitHub send the browser back to. Same variable as `ApiSettings.api_base_url`.
    api_base_url: str = "http://localhost:8000"

    @property
    def cookie_secure(self) -> bool:
        return self.api_base_url.lower().startswith("https://")

    @property
    def stripe_ready(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_webhook_secret and self.stripe_price_id)

    @property
    def signin_ready(self) -> bool:
        return bool(self.github_oauth_client_id and self.github_oauth_client_secret and self.session_secret and len(self.session_secret) >= 32)


@lru_cache
def get_billing_settings() -> BillingSettings:
    return BillingSettings()
