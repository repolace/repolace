import base64
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_REPO_ROOT_ENV_FILE, extra="ignore")

    database_url: str
    redis_url: str

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
