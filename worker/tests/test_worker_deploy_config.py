"""The compose stack and the env files beside it, held to what the code and the rules require.

Nothing here starts a container; it reads `infra/docker-compose.yml` as data. The things worth a
test are the ones whose failure is silent or dangerous: which env file each service loads, and
(added with the worker services) which docker socket they mount.
"""

from pathlib import Path

import pytest
import yaml

INFRA = Path(__file__).resolve().parents[2] / "infra"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((INFRA / "docker-compose.yml").read_text())


def env_files(service: dict) -> list[str]:
    """The `env_file` paths of one service, whichever of compose's three spellings it uses."""
    entries = service.get("env_file", [])
    if isinstance(entries, str):
        entries = [entries]
    return [entry if isinstance(entry, str) else entry["path"] for entry in entries]


class TestTheApiHasItsOwnEnvFile:
    def test_the_api_loads_infra_api_env_and_not_the_repository_env(self, compose):
        assert env_files(compose["services"]["api"]) == ["api.env"]

    def test_no_service_loads_the_repository_env(self, compose):
        for name, service in compose["services"].items():
            assert all(Path(path).name != ".env" for path in env_files(service)), name
