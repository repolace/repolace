from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class SharedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_REPO_ROOT_ENV_FILE, extra="ignore")

    database_url: str
    redis_url: str
    celery_broker_url: str
