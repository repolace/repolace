"""The compose stack and the env files beside it, held to what the code and the rules require.

Nothing here starts a container; it reads `infra/docker-compose.yml` as data. The things worth a
test are the ones whose failure is silent or dangerous: which env file each service loads, which
docker socket the workers mount, whether the sandbox's bind-mount paths mean the same thing to the
worker and to the daemon, and which queue each worker consumes.
"""

import re
from pathlib import Path

import pytest
import yaml
from repolace_gateway.config import PROVIDER_KEY_FIELDS, GatewaySettings
from repolace_pipeline.config import PipelineSettings
from repolace_worker.celery_app import celery_app
from repolace_worker.config import WorkerSettings

INFRA = Path(__file__).resolve().parents[2] / "infra"
COMPOSE_FILE = INFRA / "docker-compose.yml"
WORKER_ENV_EXAMPLE = INFRA / "worker.env.example"

WORKERS = ("worker", "worker-maintenance")
#: The three URLs compose itself sets on every service; an env file must not also carry them.
COMPOSE_OWNED = {"DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL"}
#: The host sockets that are root-equivalent. The rootless one is `${DOCKER_SOCKET:-/run/user/<uid>/docker.sock}`.
ROOTFUL_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())


def env_files(service: dict) -> list[str]:
    """The `env_file` paths of one service, whichever of compose's three spellings it uses."""
    entries = service.get("env_file", [])
    if isinstance(entries, str):
        entries = [entries]
    return [entry if isinstance(entry, str) else entry["path"] for entry in entries]


_INTERPOLATION = re.compile(r"\$\{[^}]*\}")
_MOUNT_MODES = {"ro", "rw", "z", "Z"}


def volume(entry: str) -> tuple[str, str]:
    """(host source, container target) of a short-syntax volume, mode suffix dropped.

    `${VAR:-default}` holds a colon of its own, so each interpolation is masked before splitting.
    """
    held: list[str] = []

    def mask(match: re.Match) -> str:
        held.append(match.group(0))
        return f"\0{len(held) - 1}\0"

    parts = _INTERPOLATION.sub(mask, entry).split(":")
    if len(parts) == 3 and parts[2] in _MOUNT_MODES:
        parts = parts[:2]
    source, target = (re.sub(r"\0(\d+)\0", lambda m: held[int(m.group(1))], part) for part in parts)
    return source, target


def command(service: dict) -> list[str]:
    cmd = service["command"]
    return cmd.split() if isinstance(cmd, str) else cmd


def option(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


class TestTheApiHasItsOwnEnvFile:
    def test_the_api_loads_infra_api_env_and_not_the_repository_env(self, compose):
        assert env_files(compose["services"]["api"]) == ["api.env"]

    def test_no_service_loads_the_repository_env(self, compose):
        for name, service in compose["services"].items():
            assert all(Path(path).name != ".env" for path in env_files(service)), name


class TestTheWorkerServices:
    def test_there_is_a_task_worker_and_a_maintenance_worker_from_one_image(self, compose):
        services = compose["services"]
        assert all(name in services for name in WORKERS)
        assert services["worker"]["build"] == services["worker-maintenance"]["build"]
        assert services["worker"]["image"] == services["worker-maintenance"]["image"]

    def test_each_worker_has_the_role_and_queue_the_code_expects(self, compose):
        services = compose["services"]
        assert services["worker"]["environment"]["WORKER_ROLE"] == "task"
        assert services["worker-maintenance"]["environment"]["WORKER_ROLE"] == "maintenance"
        # The task worker names the default queue explicitly so it can never also consume the
        # maintenance queue; the maintenance worker consumes only that one.
        assert option(command(services["worker"]), "-Q") == celery_app.conf.task_default_queue
        assert option(command(services["worker-maintenance"]), "-Q") == WorkerSettings().maintenance_queue

    def test_the_maintenance_worker_runs_beat_once_with_one_child(self, compose):
        argv = command(compose["services"]["worker-maintenance"])
        assert "-B" in argv
        assert option(argv, "--concurrency") == "1"
        assert "-B" not in command(compose["services"]["worker"])

    def test_only_the_task_worker_loads_provider_credentials(self, compose):
        services = compose["services"]
        assert env_files(services["worker"]) == ["worker.env"]
        assert env_files(services["worker-maintenance"]) == []
        provider_keys = {field.upper() for field in PROVIDER_KEY_FIELDS.values()} | {"GITHUB_APP_PRIVATE_KEY_BASE64"}
        assert not provider_keys & set(services["worker-maintenance"]["environment"])

    def test_both_workers_can_reach_the_database_the_cache_and_the_broker(self, compose):
        for name in WORKERS:
            assert set(compose["services"][name]["environment"]) >= COMPOSE_OWNED, name


class TestTheDockerSocket:
    def test_no_service_mounts_a_rootful_socket(self, compose):
        for name, service in compose["services"].items():
            for entry in service.get("volumes", []):
                source, _ = volume(entry)
                assert source not in ROOTFUL_SOCKETS, f"{name} mounts {source}: root-equivalent on the host"

    def test_the_rootful_socket_is_named_only_in_the_warning_comment(self, compose):
        as_data = yaml.safe_dump(compose)
        assert not any(path in as_data for path in ROOTFUL_SOCKETS)
        lines = COMPOSE_FILE.read_text().splitlines()
        naming_it = [line for line in lines if any(path in line for path in ROOTFUL_SOCKETS)]
        assert naming_it, "the warning next to the mount is missing"
        assert all(line.lstrip().startswith("#") for line in naming_it)
        assert any("root-equivalent" in line for line in lines)

    def test_both_workers_mount_the_rootless_socket_and_point_docker_at_it(self, compose):
        for name in WORKERS:
            service = compose["services"][name]
            mounts = [volume(entry) for entry in service["volumes"]]
            sockets = [(source, target) for source, target in mounts if "DOCKER_SOCKET" in source]
            assert len(sockets) == 1, name
            source, target = sockets[0]
            assert source.startswith("${DOCKER_SOCKET:-/run/user/"), "the default must be the rootless socket"
            assert service["environment"]["DOCKER_HOST"] == f"unix://{target}", name


class TestTheWorkspaceVolume:
    def test_the_task_worker_sees_the_workspace_at_the_path_the_daemon_does(self, compose):
        """The sandbox's `-v <host path>:/repo` is resolved by the daemon on the host, so the worker
        must create the task directories at a path that means the same thing in both places."""
        service = compose["services"]["worker"]
        workspace = service["environment"]["WORKSPACE_PARENT_DIR"]
        sources = [source for source, target in map(volume, service["volumes"]) if target == workspace]
        assert sources == [workspace]

    def test_the_workspace_default_is_not_the_process_temp_directory(self, compose):
        workspace = compose["services"]["worker"]["environment"]["WORKSPACE_PARENT_DIR"]
        assert re.fullmatch(r"\$\{WORKSPACE_PARENT_DIR:-/(?!tmp/)[^}]+\}", workspace), workspace


class TestWorkerEnvExample:
    def known_names(self) -> set[str]:
        gateway = {
            field.validation_alias if isinstance(field.validation_alias, str) else name.upper()
            for name, field in GatewaySettings.model_fields.items()
        }
        worker = {
            field.validation_alias if isinstance(field.validation_alias, str) else f"WORKER_{name.upper()}"
            for name, field in WorkerSettings.model_fields.items()
        }
        pipeline = {name.upper() for name in PipelineSettings.model_fields}
        return gateway | worker | pipeline

    def test_it_asks_for_what_a_task_needs_and_nothing_a_setting_does_not_read(self):
        names = set(read_env(WORKER_ENV_EXAMPLE))
        required = {field.upper() for field in PROVIDER_KEY_FIELDS.values()} | {
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY_BASE64",
        }
        assert required <= names
        assert names <= self.known_names()
        assert not names & COMPOSE_OWNED, "compose sets these; a second copy here only confuses"

    def test_every_value_is_an_empty_placeholder(self):
        assert set(read_env(WORKER_ENV_EXAMPLE).values()) == {""}
