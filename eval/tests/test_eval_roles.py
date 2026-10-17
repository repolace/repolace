"""`roles` in the benchmark harness: recorded in the run manifest, passed to every task, checked by the runner.

A sweep of the role split and a sweep of the single agent are different agents, so the manifest
must say which ran and the runner must refuse to run one under the other's manifest. A
single-agent manifest stays exactly what it was before the key existed.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness import enqueue, runner
from harness.enqueue import ManifestInputs, build_manifest, ensure_manifest
from harness.run_manifest import ManifestError, RunManifest, dump_manifest, load_manifest, manifest_path
from harness.runner import RunnerConfig, check_manifest, child_argv

RUN = "roles-run"
TASK_ID = uuid.UUID("2f8a1c4e-0000-4000-8000-000000000001")
CREATED = datetime(2026, 10, 3, tzinfo=UTC)


def manifest(**overrides) -> RunManifest:
    values = {
        "eval_run_id": RUN, "created_at": "2026-10-03T00:00:00+00:00", "git_sha": "a" * 40, "model": "claude-test",
        "stage_models": {"agent": "claude-test"}, "limits": {"runner_wall_clock_seconds": 60.0}, "runs_per_instance": 1,
        "instance_ids": ("psf__requests-1001",), "agent": "llm",
    }
    values.update(overrides)
    return RunManifest(**values)


def write_document(tmp_path: Path, document: dict) -> Path:
    directory = tmp_path / RUN
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    path.write_text(json.dumps(document))
    return path


class TestTheManifest:
    def test_roles_round_trips_and_is_written_when_true(self, tmp_path):
        path = dump_manifest(manifest(roles=True), tmp_path)

        assert json.loads(path.read_text())["roles"] is True
        assert load_manifest(path).roles is True

    def test_a_single_agent_manifest_is_byte_for_byte_what_it_was_before_the_key(self, tmp_path):
        path = dump_manifest(manifest(), tmp_path)

        assert "roles" not in json.loads(path.read_text())
        assert load_manifest(path) == manifest() and load_manifest(path).roles is False

    def test_a_manifest_written_before_the_key_reads_as_a_single_agent_run(self, tmp_path):
        document = json.loads(dump_manifest(manifest(), tmp_path).read_text())
        document.pop("roles", None)

        assert load_manifest(write_document(tmp_path, document)).roles is False

    @pytest.mark.parametrize("value", ["true", 1, None, "yes"])
    def test_roles_must_be_a_boolean(self, tmp_path, value):
        document = {**json.loads(dump_manifest(manifest(), tmp_path).read_text()), "roles": value}

        with pytest.raises(ManifestError, match="roles must be true or false"):
            load_manifest(write_document(tmp_path, document))

    @pytest.mark.parametrize("agent", ["gold", "stub"])
    def test_roles_with_an_agent_that_calls_no_model_is_refused(self, tmp_path, agent):
        document = {**json.loads(dump_manifest(manifest(agent=agent, model="none", stage_models={}), tmp_path).read_text()), "roles": True}

        with pytest.raises(ManifestError, match="only to the llm agent"):
            load_manifest(write_document(tmp_path, document))

    def test_a_misspelt_roles_is_still_an_unknown_key(self, tmp_path):
        document = {**json.loads(dump_manifest(manifest(), tmp_path).read_text()), "rolse": True}

        with pytest.raises(ManifestError, match="unknown key.*rolse"):
            load_manifest(write_document(tmp_path, document))


class TestEnqueue:
    def build(self, tmp_path, **inputs):
        return build_manifest(
            eval_run_id=RUN, git_sha="a" * 40, instance_ids=["psf__requests-1001"], runs=1,
            inputs=ManifestInputs(runs_dir=tmp_path, model="claude-test", **inputs), created_at=CREATED,
        )

    def test_roles_is_recorded_in_the_manifest(self, tmp_path):
        assert self.build(tmp_path, roles=True).roles is True
        assert self.build(tmp_path).roles is False

    def test_roles_with_the_gold_agent_is_refused(self, tmp_path):
        with pytest.raises(ManifestError, match="--roles needs --agent llm"):
            self.build(tmp_path, roles=True, agent="gold")

    def test_a_roles_sweep_under_a_single_agent_run_id_is_a_different_sweep(self, tmp_path):
        ensure_manifest(tmp_path, self.build(tmp_path))

        with pytest.raises(ManifestError, match="differs in: roles"):
            ensure_manifest(tmp_path, self.build(tmp_path, roles=True))

    def test_the_flag_parses_and_is_refused_for_gold_before_any_file_is_read(self, tmp_path, capsys):
        assert enqueue.build_parser().parse_args(["--eval-run-id", RUN, "--roles"]).roles is True
        assert enqueue.build_parser().parse_args(["--eval-run-id", RUN]).roles is False

        code = enqueue.main(["--eval-run-id", RUN, "--agent", "gold", "--roles", "--instances-dir", str(tmp_path / "none")])

        assert code == 2 and "--roles needs --agent llm" in capsys.readouterr().err


class TestTheRunner:
    def config(self, **overrides) -> RunnerConfig:
        return RunnerConfig(eval_run_id=RUN, model="claude-test", **overrides)

    def test_each_task_is_started_with_roles_when_the_run_has_them(self):
        argv = child_argv(("run-task",), TASK_ID, self.config(roles=True))

        assert argv[-1] == "--roles" and argv[:4] == ["run-task", argv[1], "--agent", "llm"]

    def test_a_single_agent_run_passes_no_roles_flag(self):
        assert "--roles" not in child_argv(("run-task",), TASK_ID, self.config())

    @pytest.mark.parametrize(("recorded", "given"), [(True, False), (False, True)])
    def test_a_runner_that_disagrees_with_the_manifest_about_roles_is_refused(self, tmp_path, recorded, given):
        inputs = ManifestInputs(runs_dir=tmp_path, model="claude-test", roles=recorded, timeout_seconds=60.0)
        ensure_manifest(tmp_path, build_manifest(
            eval_run_id=RUN, git_sha="a" * 40, instance_ids=["psf__requests-1001"], runs=1, inputs=inputs, created_at=CREATED,
        ))
        assert manifest_path(tmp_path, RUN).exists()

        with pytest.raises(ManifestError, match="roles: the manifest says"):
            check_manifest(tmp_path, self.config(roles=given, timeout_seconds=60.0))

    def test_an_agreeing_roles_run_passes(self, tmp_path):
        inputs = ManifestInputs(runs_dir=tmp_path, model="claude-test", roles=True, timeout_seconds=60.0)
        ensure_manifest(tmp_path, build_manifest(
            eval_run_id=RUN, git_sha="a" * 40, instance_ids=["psf__requests-1001"], runs=1, inputs=inputs, created_at=CREATED,
        ))

        assert check_manifest(tmp_path, self.config(roles=True, timeout_seconds=60.0)) is True

    def test_the_flag_parses_and_is_refused_for_gold(self, capsys):
        assert runner.build_parser().parse_args(["--eval-run-id", RUN, "--roles"]).roles is True

        assert runner.main(["--eval-run-id", RUN, "--agent", "gold", "--roles"]) == 2
        assert "--roles needs --agent llm" in capsys.readouterr().err
