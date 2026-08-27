"""Settings for the pipeline process.

Duplicates a few fields of `ApiSettings` on purpose. Importing `ApiSettings`
would point the dependency the wrong way -- the pipeline would depend on the
API package -- and hoisting the GitHub App fields into `SharedSettings` would
make `alembic/env.py` and `celery_app.py` require App credentials merely to
import, breaking migrations for anyone without them.

The tidy fix later is a `GithubAppSettings` in `shared/` that both compose.
"""

import base64
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_REPO_ROOT_ENV_FILE, extra="ignore")

    database_url: str
    github_app_id: str
    github_app_private_key_base64: str

    @property
    def github_app_private_key(self) -> str:
        return base64.b64decode(self.github_app_private_key_base64).decode("utf-8")


@lru_cache
def get_settings() -> PipelineSettings:
    return PipelineSettings()
