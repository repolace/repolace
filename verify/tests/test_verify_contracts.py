"""The Wave 0 contracts: the shapes `ScriptResult`, run labels and `Verdict` promise."""

import re
import uuid
from dataclasses import FrozenInstanceError, fields

import pytest

from verify.protocol import ScriptResult
from verify.scoring import Verdict
from verify.stage import container_name


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

    def test_no_exit_code_with_no_explanation_is_refused(self):
        """A result that says nothing: it did not finish, was not killed and did not
        fail to start. A tool rendering it would have to invent a sentence."""
        with pytest.raises(ValueError, match="must say why"):
            ScriptResult(exit_code=None)

    @pytest.mark.parametrize("empty", ["", None])
    def test_an_empty_error_does_not_explain_a_missing_exit_code(self, empty):
        with pytest.raises(ValueError, match="must say why"):
            ScriptResult(exit_code=None, error=empty)

    def test_a_timeout_explains_it(self):
        assert ScriptResult(exit_code=None, timed_out=True).timed_out

    def test_a_runtime_error_explains_it(self):
        assert ScriptResult(exit_code=None, error="docker unreachable").error == "docker unreachable"

    @pytest.mark.parametrize("code", [0, 1, 2, 137, -9])
    def test_an_exit_code_needs_no_explanation(self, code):
        assert ScriptResult(exit_code=code).exit_code == code

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

    def test_a_label_may_be_exactly_64_characters(self):
        label = "p" + "a" * 63

        assert len(label) == 64
        assert container_name(uuid.uuid4(), label).endswith(f"-{label}")

    @pytest.mark.parametrize("length", [65, 100, 300])
    def test_a_longer_label_is_refused_because_it_becomes_a_directory_name(self, length):
        """The filesystem limit is 255 bytes per component and Docker has none, so an
        over-long label would pass here and fail at `mkdir` with ENAMETOOLONG."""
        with pytest.raises(ValueError, match="at most 64"):
            container_name(uuid.uuid4(), "p" + "a" * (length - 1))

    @pytest.mark.parametrize("label", ["1", "42", "", "-x", "probe/3", "probe 3", "probe:3"])
    def test_a_label_that_could_collide_is_refused_not_rewritten(self, label):
        """A numeric label would share a name with the int attempt; a character
        Docker rejects would be rewritten, letting two labels share one name."""
        with pytest.raises(ValueError, match="run label"):
            container_name(uuid.uuid4(), label)


class TestVerdict:
    def test_the_evidence_sets_default_to_empty(self):
        verdict = Verdict(ok=True, reason="no harm found")

        assert verdict.regressions == () and verdict.neutralized == ()
        assert verdict.new_collect_failures == () and verdict.disqualified == ()

    def test_its_fields_are_the_ones_the_contract_names(self):
        assert [f.name for f in fields(Verdict)] == [
            "ok", "reason", "regressions", "neutralized", "new_collect_failures", "disqualified",
        ]
