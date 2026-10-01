"""The Wave 0 contracts that exist before anything implements them.

Most of what is asserted here is that the stubs *refuse loudly*. A stub that
returned a plausible empty value instead of raising would let a stream build on
a seam that does nothing, and the first symptom would be a benchmark number, not
an error. Each of these tests is deleted or rewritten by the stream named in its
message, which is the point of naming it.
"""

import re
import uuid
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from verify.backends.docker import DockerBackend
from verify.overlay import apply_overlay
from verify.protocol import EnvironmentRef, RepoSpec, ScriptResult, SuiteResult
from verify.scoring import Verdict
from verify.stage import container_name

pytestmark = pytest.mark.anyio


class TestScriptResult:
    def test_only_the_exit_code_is_required(self):
        result = ScriptResult(exit_code=0)

        assert result.stdout == "" and result.stderr == ""
        assert result.timed_out is False and result.truncated is False
        assert result.duration_seconds is None and result.error is None

    def test_a_script_that_never_ran_has_no_exit_code(self):
        """`error` is "the runtime failed", which is not a non-zero exit."""
        result = ScriptResult(exit_code=None, error="docker unreachable")

        assert result.exit_code is None
        assert result.error == "docker unreachable"

    def test_it_is_immutable(self):
        with pytest.raises(FrozenInstanceError):
            ScriptResult(exit_code=0).exit_code = 1  # type: ignore[misc]

    def test_its_fields_are_the_ones_the_contract_names(self):
        assert [f.name for f in fields(ScriptResult)] == [
            "exit_code", "stdout", "stderr", "timed_out", "truncated", "duration_seconds", "error",
        ]


class TestContainerNameLabels:
    def test_an_int_attempt_gives_the_name_it_always_did(self):
        task = uuid.uuid4()

        assert container_name(task, 0) == f"repolace-{task.hex[:12]}-0"
        assert container_name(task, 3) == f"repolace-{task.hex[:12]}-3"

    def test_a_str_label_is_distinct_from_every_attempt_and_from_other_labels(self):
        task = uuid.uuid4()
        names = {
            container_name(task, 0),
            container_name(task, 1),
            container_name(task, "probe-1"),
            container_name(task, "probe-2"),
            container_name(task, "script-1"),
        }

        assert len(names) == 5

    def test_a_label_is_a_legal_docker_name(self):
        assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", container_name(uuid.uuid4(), "probe-3"))

    @pytest.mark.parametrize("label", ["1", "42", "", "-x", "probe/3", "probe 3", "probe:3"])
    def test_a_label_that_could_collide_is_refused_not_rewritten(self, label):
        """A numeric label would share a name with the int attempt; a character
        Docker rejects would be rewritten, letting two labels share one name."""
        with pytest.raises(ValueError, match="run label"):
            container_name(uuid.uuid4(), label)


class TestStubsRefuseLoudly:
    def test_apply_overlay(self, tmp_path):
        with pytest.raises(NotImplementedError, match="stream A"):
            apply_overlay(tmp_path, {"tests/test_x.py": b"x"}, dir_mode=0o777)

    async def test_docker_run_script(self, tmp_path: Path):
        with pytest.raises(NotImplementedError, match="stream A"):
            await DockerBackend().run_script(
                EnvironmentRef("docker", "img"),
                tmp_path,
                tmp_path / "s.py",
                RepoSpec(key="a/b"),
                container_name="c",
                timeout_seconds=1.0,
            )


class TestVerdict:
    def test_the_evidence_sets_default_to_empty(self):
        verdict = Verdict(ok=True, reason="no harm found")

        assert verdict.regressions == () and verdict.neutralized == ()
        assert verdict.new_collect_failures == () and verdict.disqualified == ()

    def test_its_fields_are_the_ones_the_contract_names(self):
        assert [f.name for f in fields(Verdict)] == [
            "ok", "reason", "regressions", "neutralized", "new_collect_failures", "disqualified",
        ]
