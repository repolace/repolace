"""The app host's files (bootstrap, deploy, compose, workflow), checked statically. Nothing here starts a
container or touches AWS: it pins the properties that must not regress, each one a decision recorded in the
files' own comments."""

import re
from pathlib import Path

import yaml

AWS = Path(__file__).resolve().parents[1]
ROOT = AWS.parents[1]
COMPOSE = yaml.safe_load((AWS / "compose.app.yml").read_text())
SERVICES = COMPOSE["services"]
BOOTSTRAP = (AWS / "bootstrap-app.sh").read_text()
DEPLOY = (AWS / "deploy-app.sh").read_text()
WORKFLOW = (ROOT / ".github/workflows/deploy-app.yml").read_text()


def test_no_service_publishes_a_host_port():
    assert [name for name, svc in SERVICES.items() if "ports" in svc] == []


def test_the_rootful_docker_socket_is_never_mounted():
    for name, svc in SERVICES.items():
        for volume in svc.get("volumes", []):
            assert "/var/run/docker.sock" not in volume, name
            assert not volume.startswith("/run/docker.sock"), name


def test_only_workers_reach_the_daemon_and_only_by_the_rootless_socket():
    reaching = {n for n, s in SERVICES.items() if any("docker.sock" in v for v in s.get("volumes", []))}
    assert reaching == {"worker", "worker-maintenance"}
    for name in reaching:
        assert any(v.startswith("${DOCKER_SOCKET:?}:") for v in SERVICES[name]["volumes"])


def test_provider_keys_reach_the_task_worker_only():
    assert SERVICES["worker"]["env_file"] == ["worker.env"]
    for name in ("api", "worker-maintenance", "migrate", "postgres", "redis", "rabbitmq", "cloudflared"):
        assert "worker.env" not in SERVICES[name].get("env_file", []), name
    assert "worker.env" not in SERVICES["api"]["env_file"]
    assert "env_file" not in SERVICES["worker-maintenance"]


def test_deploy_refuses_a_provider_or_cloud_credential_in_the_api_secret():
    assert re.search(r"ANTHROPIC\|OPENAI\|GEMINI.*VERTEX\|LITELLM\|BEDROCK\|AWS_", DEPLOY)
    assert "move it to ${WORKER_ENV_SECRET}" in DEPLOY


def test_stores_bind_mount_from_the_retained_data_volume():
    for name, subdir in (("postgres", "postgres"), ("redis", "redis"), ("rabbitmq", "rabbitmq")):
        assert any(v.startswith("${DATA_DIR:?}/" + subdir + ":") for v in SERVICES[name]["volumes"]), name


def test_the_workspace_is_mounted_at_the_same_path_inside_and_out():
    mounts = [v for v in SERVICES["worker"]["volumes"] if "WORKSPACE_PARENT_DIR" in v]
    assert mounts == ["${WORKSPACE_PARENT_DIR:?}:${WORKSPACE_PARENT_DIR:?}"]
    assert SERVICES["worker"]["environment"]["WORKSPACE_PARENT_DIR"] == "${WORKSPACE_PARENT_DIR:?}"


def test_the_api_and_workers_wait_for_the_migration():
    for name in ("api", "worker", "worker-maintenance"):
        assert SERVICES[name]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert SERVICES["migrate"]["restart"] == "no"


def test_the_stores_are_not_overridable_by_the_secret_files():
    # DATABASE_URL, REDIS_URL and CELERY_BROKER_URL are set in `environment:`, which beats `env_file:`.
    for name in ("api", "worker", "worker-maintenance", "migrate"):
        env = SERVICES[name]["environment"]
        assert {"DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL"} <= set(env), name


def test_exactly_one_maintenance_replica_with_beat():
    cmd = SERVICES["worker-maintenance"]["command"]
    assert "-B" in cmd and "repolace.maintenance" in cmd
    assert "deploy" not in SERVICES["worker-maintenance"]


def test_bootstrap_blocks_the_metadata_service_for_the_docker_user_and_proves_it():
    assert "meta skuid ${uid} ip daddr 169.254.169.254" in BOOTSTRAP
    assert "fd00:ec2::254" in BOOTSTRAP
    assert "169.254.169.253" in BOOTSTRAP  # named as deliberately not blocked (the VPC resolver)
    assert "NOT safe to run tasks" in BOOTSTRAP
    # the proof must be able to fail: a session token request (a bare GET gets 401 either way), a positive control,
    # and the rule's packet counter
    assert "/latest/api/token" in BOOTSTRAP
    assert "archive.ubuntu.com" in BOOTSTRAP
    assert "counter packets [1-9]" in BOOTSTRAP


def test_no_metadata_block_means_no_rootless_daemon():
    assert "Requires=repolace-imds-block.service" in BOOTSTRAP
    assert "systemctl mask nftables.service" in BOOTSTRAP  # its `flush ruleset` would remove the table


def test_bootstrap_delegates_cgroups_and_proves_the_limits():
    assert "Delegate=cpu cpuset io memory pids" in BOOTSTRAP
    assert "--memory 64m --pids-limit 32" in BOOTSTRAP
    assert '"67108864 32 "' in BOOTSTRAP  # read back from inside the container: the flags are dropped silently


def test_bootstrap_keeps_the_rootful_daemon_off_and_demands_a_rootless_one():
    assert "systemctl mask --now docker.service docker.socket" in BOOTSTRAP
    assert "*rootless*" in BOOTSTRAP


def test_bootstrap_never_formats_a_volume_that_has_a_filesystem():
    assert re.search(r"if ! blkid .*\n\s*mkfs\.ext4", BOOTSTRAP)


def test_the_user_manager_waits_for_all_three_mounts():
    assert "RequiresMountsFor=%s %s %s" in BOOTSTRAP


def test_bootstrap_writes_its_marker_last():
    assert BOOTSTRAP.rstrip().splitlines()[-2].endswith('"${APP_DIR}/bootstrap.ok"')


def test_deploy_needs_the_marker_the_block_and_the_mounts():
    assert "bootstrap.ok" in DEPLOY
    assert "repolace_imds" in DEPLOY
    assert "mountpoint -q" in DEPLOY


def test_deploy_reaches_the_rootless_daemon_by_its_socket_and_never_the_rootful_one():
    assert 'DOCKER_HOST=unix:///run/user/${uid}/docker.sock' in DEPLOY
    assert not re.search(r"^\s*(docker|docker compose) ", DEPLOY, re.MULTILINE), "a bare docker call would use the wrong daemon"
    assert "/var/run/docker.sock" not in DEPLOY


def test_secret_files_are_root_only_except_the_tunnel_credentials():
    assert '"${APP_DIR}/api.env" "root:root" 0600' in DEPLOY
    assert '"${APP_DIR}/worker.env" "root:root" 0600' in DEPLOY
    assert '"${APP_DIR}/stack.env"' in DEPLOY and "chmod 0600" in DEPLOY
    assert '"${RUN_USER}:${RUN_USER}" 0400' in DEPLOY  # cloudflared reads it as that user


def test_a_secret_is_validated_before_it_is_moved_into_place():
    assert DEPLOY.index("check_env_file \"${APP_DIR}/api.env.tmp\"") < DEPLOY.index('mv "${APP_DIR}/api.env.tmp"')
    assert DEPLOY.index("holds a model provider or cloud credential") < DEPLOY.index('mv "${APP_DIR}/api.env.tmp"')


def test_deploy_takes_a_lock_and_sets_its_own_path():
    assert "flock -n 9" in DEPLOY
    assert "/snap/bin" in DEPLOY


def test_deploy_copies_files_out_of_the_image_as_a_tar_stream():
    assert 'docker cp "${cid}:/repo/infra/aws/." - | tar -x' in DEPLOY


def test_deploy_migrates_alone_before_the_new_version_starts():
    assert DEPLOY.index("compose run -T --rm --no-deps migrate") < DEPLOY.index("if ! compose up -d --remove-orphans >/dev/null")


def test_deploy_proves_the_version_and_rolls_back():
    assert "api_sha_local" in DEPLOY and "api_sha_public" in DEPLOY
    assert "rollback()" in DEPLOY
    assert DEPLOY.index("rollback()") < DEPLOY.index('log "starting the app"')


def test_deploy_never_prunes_every_image():
    code = "\n".join(line for line in DEPLOY.splitlines() if not line.lstrip().startswith("#"))
    assert "prune --all" not in code and "prune -a" not in code, "the sandbox's per-environment images live on the same daemon"


def test_the_api_health_endpoint_reports_the_sha():
    assert "REPOLACE_GIT_SHA" in (ROOT / "api/repolace_api/main.py").read_text()


def test_the_image_carries_what_deploy_copies_out_of_it():
    ignore = (ROOT / ".dockerignore").read_text()
    assert "!infra/aws" in ignore and "!infra/rabbitmq.conf" in ignore


def test_workflow_is_gated_reviewed_and_pinned():
    assert "vars.APP_DEPLOY_ENABLED == 'true'" in WORKFLOW
    assert "environment: production-app" in WORKFLOW
    assert "concurrency:" in WORKFLOW and "cancel-in-progress: false" in WORKFLOW
    uses = re.findall(r"uses: (\S+)", WORKFLOW)
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), "pin every action to a sha"


def test_workflow_triggers_on_the_files_the_host_runs():
    for path in ('"worker/**"', '"api/**"', '"infra/aws/compose.app.yml"', '"infra/aws/deploy-app.sh"'):
        assert path in WORKFLOW


def test_workflow_uses_no_stored_secret():
    assert "secrets." not in WORKFLOW
