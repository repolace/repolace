"""The agent contracts: shapes, invariants, and that importing them stays cheap."""

import dataclasses
import subprocess
import sys
import typing
import uuid
from pathlib import Path

import pytest

from repolace_agents.contracts import (
    AgentDeps,
    AgentLimits,
    AgentResult,
    AgentRunner,
    AttemptRecord,
    IssueContext,
    LLMClientLike,
    SearchHit,
    StopReason,
)
from repolace_agents.run import run_agent
from repolace_agents.tools.base import ToolContext, ToolLimits, build_toolbox
from repolace_shared.db.models import AGENT_STOP_REASONS
from verify.protocol import SuiteResult
from verify.scoring import is_protected_path

pytestmark = pytest.mark.anyio


class TestStopReason:
    def test_the_values_are_exactly_what_the_database_accepts(self):
        """Held equal to the CHECK constraint's list, because the two are
        maintained in different packages that cannot import each other."""
        assert {reason.value for reason in StopReason} == set(AGENT_STOP_REASONS)

    def test_the_declared_order_matches_the_database_list(self):
        assert tuple(reason.value for reason in StopReason) == AGENT_STOP_REASONS

    def test_it_is_a_str_so_it_can_be_written_to_a_varchar_column(self):
        assert StopReason.STEP_CAP == "step_cap"
        assert StopReason("llm_error") is StopReason.LLM_ERROR

    def test_running_out_of_attempts_still_red_is_its_own_reason(self):
        """Without it "submitted clean" and "ran out of attempts still red" both
        read `submitted`, and the report cannot tell them apart."""
        assert StopReason.MAX_ATTEMPTS.value == "max_attempts"
        assert StopReason.MAX_ATTEMPTS in set(StopReason)
        assert len(StopReason) == 8


class TestAgentLimits:
    def test_the_defaults_are_the_planned_ones(self):
        limits = AgentLimits()

        assert limits.max_attempts == 3
        assert limits.max_steps_per_attempt == 40
        assert limits.max_issue_chars == 12_000
        assert limits.max_context_snippet_lines == 60

    def test_it_is_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            AgentLimits().max_attempts = 99  # type: ignore[misc]


class TestAgentResult:
    def result(self, stop_reason=StopReason.SUBMITTED, **kw):
        return AgentResult(
            stop_reason=stop_reason,
            summary=kw.pop("summary", None),
            attempts=kw.pop("attempts", 1),
            steps=kw.pop("steps", 3),
            last_attempt=kw.pop("last_attempt", None),
        )

    def test_a_submission_is_submitted(self):
        assert self.result(StopReason.SUBMITTED, summary="done").submitted is True

    @pytest.mark.parametrize("reason", [r for r in StopReason if r is not StopReason.SUBMITTED])
    def test_any_other_reason_is_not_a_submission(self, reason):
        assert self.result(reason).submitted is False

    @pytest.mark.parametrize("reason", list(StopReason))
    def test_submitted_is_derived_from_the_reason_for_every_reason(self, reason):
        """One source of truth: there is no field to disagree with."""
        assert self.result(reason).submitted is (reason is StopReason.SUBMITTED)

    def test_submitted_is_not_a_field(self):
        """It used to be one, forced to agree with `stop_reason` in `__post_init__`,
        and that raised on legitimate sequences."""
        assert "submitted" not in {f.name for f in dataclasses.fields(AgentResult)}
        with pytest.raises(TypeError):
            AgentResult(  # type: ignore[call-arg]
                stop_reason=StopReason.SUBMITTED, submitted=True, summary=None, attempts=1, steps=1, last_attempt=None
            )

    def test_submitted_cannot_be_assigned(self):
        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            self.result().submitted = False  # type: ignore[misc]

    def test_an_agent_that_submits_but_changed_nothing_is_a_legitimate_result(self):
        """"Submit, then no net change" is NO_CHANGE. With a stored `submitted` flag
        this raised ValueError and the task was FAILED -- a normal outcome treated as
        repolace breaking."""
        result = self.result(StopReason.NO_CHANGE, attempts=0, summary="nothing to do")

        assert result.submitted is False and result.summary == "nothing to do"

    def test_a_retry_that_later_hit_a_budget_is_a_legitimate_result(self):
        attempt = AttemptRecord(1, "abc123", SuiteResult(), infrastructure_error=False)

        result = self.result(StopReason.BUDGET_USD, attempts=1, last_attempt=attempt)

        assert result.submitted is False and result.last_attempt is attempt

    @pytest.mark.parametrize("bad", ["submitted", "step_cap", "bogus", "", None, 3, b"submitted"])
    def test_a_stop_reason_that_is_not_a_stop_reason_is_refused(self, bad):
        """A plain string passes every comparison, then breaks on `.value` far from
        where it was built -- or, worse, is first caught by the database CHECK."""
        with pytest.raises(TypeError, match="stop_reason must be a StopReason"):
            self.result(bad)

    def test_the_value_of_a_real_stop_reason_is_what_the_column_stores(self):
        assert self.result(StopReason.MAX_ATTEMPTS).stop_reason.value == "max_attempts"

    def test_it_carries_the_last_attempt(self):
        attempt = AttemptRecord(1, "abc123", SuiteResult(passed=("t::a",)), infrastructure_error=False)

        assert self.result(StopReason.SUBMITTED, last_attempt=attempt).last_attempt is attempt

    def test_it_is_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            self.result().attempts = 9  # type: ignore[misc]

    def test_its_fields_are_the_ones_the_contract_names(self):
        assert [f.name for f in dataclasses.fields(AgentResult)] == [
            "stop_reason", "summary", "attempts", "steps", "last_attempt",
        ]


class TestAgentDeps:
    def deps(self, **overrides) -> AgentDeps:
        async def verify_attempt(attempt: int):
            return None

        async def changed_files():
            return []

        fields = {
            "llm": None,
            "tools": None,
            "checkout": Path("/tmp/checkout"),
            "issue": IssueContext(7, "title", None, "https://example.test/7", None),
            "retrieved": (),
            "repo_overview": "",
            "baseline": SuiteResult(),
            "baseline_files": (),
            "hidden_paths": frozenset(),
            "verify_attempt": verify_attempt,
            "changed_files": changed_files,
        }
        return AgentDeps(**{**fields, **overrides})

    def test_limits_default_to_the_planned_bounds(self):
        assert self.deps().limits == AgentLimits()

    def test_it_is_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            self.deps().checkout = Path("/elsewhere")  # type: ignore[misc]

    def test_its_annotations_resolve_at_runtime(self):
        """LangGraph reads a context schema's hints, and a `TYPE_CHECKING`-only
        name in one would surface as an obscure NameError inside the graph."""
        hints = typing.get_type_hints(AgentDeps)

        assert set(hints) == {f.name for f in dataclasses.fields(AgentDeps)}

    def test_an_llm_is_optional_for_the_stub_and_gold_runners(self):
        assert self.deps(llm=None).llm is None

    def test_hidden_paths_default_shape_is_a_frozenset(self):
        assert self.deps().hidden_paths == frozenset()


class TestSearchHitAndIssueContext:
    def test_a_hit_has_the_fields_retrieval_returns(self):
        hit = SearchHit("a.py", 1, 9, "parse", "function", 0.5, "def parse(): ...")

        assert (hit.file_path, hit.start_line, hit.end_line) == ("a.py", 1, 9)
        assert (hit.symbol, hit.chunk_type, hit.score, hit.snippet) == (
            "parse", "function", 0.5, "def parse(): ...",
        )

    def test_an_issue_may_have_no_body_and_no_instance(self):
        issue = IssueContext(number=3, title="t", body=None, url="u", instance_id=None)

        assert issue.body is None and issue.instance_id is None


class TestToolContext:
    @staticmethod
    def context(**kw) -> ToolContext:
        async def noop(*args):
            return None

        return ToolContext(Path("."), noop, noop, None, None, **kw)

    def test_limits_default(self):
        ctx = self.context()

        assert ctx.limits == ToolLimits()
        assert ctx.run_subset is None and ctx.run_script is None

    def test_the_write_guard_defaults_to_the_scorers_function(self):
        """Additive and defaulted: every stream that builds a `ToolContext` the old
        way keeps working, and gets the baseline-blind guard."""
        ctx = self.context()

        assert ctx.is_protected is is_protected_path
        assert ctx.is_protected("tests/test_a.py") is True
        assert ctx.is_protected("src/app.py") is False

    def test_the_old_positional_shape_still_constructs(self):
        """`is_protected` comes after `limits`, so no existing positional call shifts."""
        async def noop(*args):
            return None

        ctx = ToolContext(Path("."), noop, noop, None, None, ToolLimits(max_grep_results=7))

        assert ctx.limits.max_grep_results == 7
        assert ctx.is_protected is is_protected_path

    def test_the_pipeline_can_supply_a_baseline_aware_closure(self):
        """The reason the field exists: a file pytest collects through a custom
        `python_files` is invisible to the heuristic and visible to the baseline."""
        collected = ("checks/check_foo.py",)

        def baseline_aware(path: str) -> bool:
            return is_protected_path(path, collected_files=collected)

        ctx = self.context(is_protected=baseline_aware)

        assert ctx.is_protected("checks/check_foo.py") is True
        assert self.context().is_protected("checks/check_foo.py") is False


class TestToolLimits:
    def test_the_defaults_are_the_planned_ones(self):
        limits = ToolLimits()

        assert limits.max_output_chars == 8000
        assert limits.max_read_lines == 400
        assert limits.max_file_bytes == 1_000_000
        assert limits.max_edit_chars == 20_000
        assert limits.max_create_chars == 100_000
        assert limits.max_grep_results == 200
        assert limits.max_targets == 20
        assert limits.default_script_timeout == 60.0
        assert limits.max_script_timeout == 120.0
        assert limits.max_probe_seconds == 300.0

    def test_a_probe_has_a_timeout_policy_like_a_script(self):
        """`run_tests` had none until now; only scripts did."""
        assert ToolLimits().max_probe_seconds > 0

    def test_the_default_script_timeout_is_within_the_maximum(self):
        limits = ToolLimits()

        assert limits.default_script_timeout <= limits.max_script_timeout


class TestStubsRefuseLoudly:
    async def test_run_agent_lands_in_stream_f(self):
        deps = TestAgentDeps().deps()

        with pytest.raises(NotImplementedError, match="stream F: graph"):
            await run_agent(deps)

    def test_build_toolbox_lands_in_stream_b(self):
        async def noop(*args):
            return None

        with pytest.raises(NotImplementedError, match="stream B: toolbox"):
            build_toolbox(ToolContext(Path("."), noop, noop, None, None))


def test_the_protocols_are_importable_names():
    """Names other streams import; a rename would otherwise surface far from here."""
    assert LLMClientLike is not None and AgentRunner is not None


class TestImportsStayCheap:
    def run_python(self, code: str) -> subprocess.CompletedProcess:
        # A subprocess because other tests in this process may already have
        # imported litellm (the conftest pins its price map precisely because
        # some do), which would make an in-process check meaningless.
        return subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
        )

    def test_the_contract_modules_do_not_import_litellm(self):
        result = self.run_python(
            "import repolace_agents.contracts, repolace_agents.tools.base, sys; "
            "assert 'litellm' not in sys.modules, 'litellm was imported'"
        )

        assert result.returncode == 0, result.stderr

    def test_nor_the_gateway_nor_rag_nor_torch(self):
        """`agents` depends on the gateway for its types but must not import it at
        runtime, and must never reach `rag`, which would pull torch into every
        agent test."""
        result = self.run_python(
            "import repolace_agents.contracts, repolace_agents.tools, "
            "repolace_agents.tools.base, repolace_agents.run, sys; "
            "bad = [m for m in ('litellm', 'repolace_gateway', 'retrieval', 'torch', 'sentence_transformers') "
            "if m in sys.modules]; "
            "assert not bad, f'imported: {bad}'"
        )

        assert result.returncode == 0, result.stderr
