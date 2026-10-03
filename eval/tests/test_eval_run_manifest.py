"""`harness.run_manifest`: the planned grid, written before the run and read by the report."""

import json
import stat
from pathlib import Path

import pytest

from harness.run_manifest import (
    AGENTS,
    MANIFEST_FILENAME,
    ManifestError,
    RunManifest,
    dump_manifest,
    load_manifest,
    manifest_path,
)


def manifest(**overrides) -> RunManifest:
    values = dict(
        eval_run_id="sonnet-1", created_at="2026-10-03T12:00:00+00:00", git_sha="a" * 40,
        model="anthropic/claude-sonnet-5-5", stage_models={"agent": "anthropic/claude-sonnet-5-5"},
        limits={"max_usd": 2.0, "wall_seconds": 3600, "note": None}, runs_per_instance=3,
        instance_ids=("psf__requests-1001", "psf__requests-1002"), agent="llm",
    )
    values.update(overrides)
    return RunManifest(**values)


def document(**overrides) -> dict:
    base = {
        "eval_run_id": "sonnet-1", "created_at": "2026-10-03T12:00:00+00:00", "git_sha": "a" * 40,
        "model": "anthropic/claude-sonnet-5-5", "stage_models": {"agent": "m"}, "limits": {"max_usd": 2.0},
        "runs_per_instance": 3, "instance_ids": ["psf__requests-1001"], "agent": "llm",
    }
    base.update(overrides)
    return base


def write(tmp_path: Path, data, run_id: str = "sonnet-1") -> Path:
    directory = tmp_path / run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANIFEST_FILENAME
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


class TestRoundTrip:
    def test_dump_then_load_returns_the_same_manifest(self, tmp_path):
        original = manifest()
        path = dump_manifest(original, tmp_path)
        assert path == tmp_path / "sonnet-1" / "manifest.json"
        assert load_manifest(path) == original

    def test_the_planned_grid_is_every_instance_times_every_run_index(self):
        grid = manifest().planned_pairs()
        assert len(grid) == 2 * 3
        assert ("psf__requests-1002", 2) in grid and ("psf__requests-1002", 3) not in grid

    def test_the_file_is_stable_and_readable_by_another_uid(self, tmp_path):
        path = dump_manifest(manifest(), tmp_path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        assert path.read_text() == path.read_text().rstrip("\n") + "\n"
        assert list(path.parent.glob(".manifest.json.*")) == []

    def test_dumping_twice_replaces_the_file(self, tmp_path):
        dump_manifest(manifest(), tmp_path)
        dump_manifest(manifest(runs_per_instance=1), tmp_path)
        assert load_manifest(tmp_path / "sonnet-1" / "manifest.json").runs_per_instance == 1

    def test_every_agent_kind_is_accepted(self, tmp_path):
        for agent in AGENTS:
            path = dump_manifest(manifest(agent=agent, eval_run_id=f"run-{agent}"), tmp_path)
            assert load_manifest(path).agent == agent


class TestRefusals:
    def test_an_unknown_key(self, tmp_path):
        path = write(tmp_path, document(instance_idz=["x-1"]))
        with pytest.raises(ManifestError, match="unknown key.*instance_idz"):
            load_manifest(path)

    def test_a_missing_key_names_it(self, tmp_path):
        data = document()
        del data["runs_per_instance"]
        with pytest.raises(ManifestError, match="missing key.*runs_per_instance"):
            load_manifest(write(tmp_path, data))

    @pytest.mark.parametrize(
        "field,value,message",
        [
            ("runs_per_instance", 0, "integer >= 1"),
            ("runs_per_instance", True, "integer >= 1"),
            ("runs_per_instance", "3", "integer >= 1"),
            ("instance_ids", [], "non-empty list"),
            ("instance_ids", "psf__requests-1001", "non-empty list"),
            ("instance_ids", ["../escape-1"], "instance_id"),
            ("instance_ids", ["psf__requests-1001", "psf__requests-1001"], "repeats psf__requests-1001"),
            ("agent", "human", "agent must be one of"),
            ("created_at", "yesterday", "not an ISO 8601 timestamp"),
            ("created_at", 5, "ISO 8601 string"),
            ("model", "has space", "without whitespace"),
            ("model", "", "non-empty"),
            ("git_sha", "", "non-empty"),
            ("stage_models", {"agent": 1}, "strings to strings"),
            ("stage_models", ["agent"], "strings to strings"),
            ("limits", {"nested": {"a": 1}}, "JSON scalars"),
            ("limits", [1], "JSON scalars"),
            ("eval_run_id", "../x", "eval_run_id"),
            ("eval_run_id", "a b", "eval_run_id"),
        ],
    )
    def test_a_bad_value(self, tmp_path, field, value, message):
        run_id = value if field == "eval_run_id" and isinstance(value, str) and "/" not in value and " " not in value else "sonnet-1"
        with pytest.raises(ManifestError, match=message):
            load_manifest(write(tmp_path, document(**{field: value}), run_id))

    def test_a_manifest_under_another_runs_directory_is_refused(self, tmp_path):
        path = write(tmp_path, document(eval_run_id="sonnet-1"), run_id="gpt-1")
        with pytest.raises(ManifestError, match="eval_run_id is 'sonnet-1' but the manifest sits in 'gpt-1'"):
            load_manifest(path)

    def test_not_an_object(self, tmp_path):
        with pytest.raises(ManifestError, match="top level must be a JSON object"):
            load_manifest(write(tmp_path, "[1, 2]"))

    def test_not_json(self, tmp_path):
        with pytest.raises(ManifestError, match="not valid JSON"):
            load_manifest(write(tmp_path, "{nope"))

    def test_a_missing_file(self, tmp_path):
        with pytest.raises(ManifestError, match="cannot read"):
            load_manifest(tmp_path / "ghost" / "manifest.json")

    def test_dumping_an_invalid_manifest_writes_nothing(self, tmp_path):
        with pytest.raises(ManifestError, match="integer >= 1"):
            dump_manifest(manifest(runs_per_instance=0), tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_dumping_an_invalid_run_id_writes_nothing(self, tmp_path):
        with pytest.raises(ManifestError, match="eval_run_id"):
            dump_manifest(manifest(eval_run_id="../x"), tmp_path)
        assert list(tmp_path.iterdir()) == []


class TestPath:
    def test_the_path_is_under_the_runs_directory(self, tmp_path):
        assert manifest_path(tmp_path, "sonnet-1") == tmp_path.resolve() / "sonnet-1" / "manifest.json"

    @pytest.mark.parametrize("run_id", ["../x", "a/b", "", ".hidden", "x..y"])
    def test_an_id_that_could_name_another_directory_is_refused(self, tmp_path, run_id):
        with pytest.raises(ManifestError, match="eval_run_id"):
            manifest_path(tmp_path, run_id)

    def test_a_symlinked_run_directory_is_refused_not_followed(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "manifest.json").write_text(json.dumps(document(eval_run_id="sonnet-1")))
        runs = tmp_path / "runs"
        runs.mkdir()
        (runs / "sonnet-1").symlink_to(outside)
        with pytest.raises(ManifestError, match="outside the tree"):
            manifest_path(runs, "sonnet-1")
