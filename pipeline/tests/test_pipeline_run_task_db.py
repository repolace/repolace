"""`run_task`, end to end, against a real database and a real git remote.

This is the test the project's own history asks for. The one defect that ever reached a real run
was a `MissingGreenlet` in a database path with no coverage, while every other test avoided the
database; and `run_task` had never been exercised as a whole at all. So nothing about the
pipeline is faked except the three things that cannot run here: the sandbox (the shared
`FakeBackend`), GitHub (`FakeGithubClient`) and the model (a scripted `AgentRunner`). The clone,
the index, retrieval, the real scorer and verdict, the real gate, squash, push and every
database write are the real ones.

Each test states the *column* it is about, because a terminal write that forgets a column is the
failure that has already happened once (`retry_count`, `cost_usd`, `patch_sha` and `changed_files`
were never written by anything).
"""

import re
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import text

import repolace_pipeline.run as run_module
from repolace_gateway.budget import BudgetExceeded, BudgetLimit, TaskBudget, current_scope
from repolace_gateway.errors import LLMCallError, UnpricedModelError
from repolace_gateway.recorder import CallRecord, Recorder
from repolace_gateway.redaction import Redactor
from repolace_shared.db.models import TaskOutcome, TaskStatus
from repolace_shared.git import agent_branch_name, task_workspace
from repolace_shared.instances import dump_instance
from retrieval.index import _repo_lock_key
from verify.errors import EnvironmentBuildFailed
from verify.protocol import ScriptResult, SuiteResult
from verify.testing import FakeBackend

from repolace_agents.contracts import StopReason
from repolace_pipeline.run import RunResult, run_task
from repolace_pipeline.errors import TaskNotClaimable
from repolace_pipeline.runners import GoldAgent

from pipeline_support import (
    AFTER,
    APP_SOURCE,
    BASELINE,
    VISIBLE_TEST,
    FakeGithubClient,
    FakeToolCall,
    NeverCalledLLM,
    ScriptedAgent,
    agent_result,
    edit,
    git,
    local_workspace_factory,
    make_instance,
    reload,
    seed_task,
)

pytestmark = [pytest.mark.anyio, pytest.mark.db, pytest.mark.usefixtures("embedder")]

EDITED_APP = APP_SOURCE + "\n# edited by the agent\n"
HIDDEN_TEST = "tests/test_hidden.py::test_empty_config"

#: A benchmark baseline: the curated test is red, and collected because the overlay is on disk.
BENCH_BASELINE = SuiteResult(
    passed=(VISIBLE_TEST,), failed=(HIDDEN_TEST,), collected_files=("tests/test_app.py", "tests/test_hidden.py")
)
BENCH_AFTER = SuiteResult(
    passed=(VISIBLE_TEST, HIDDEN_TEST), collected_files=("tests/test_app.py", "tests/test_hidden.py")
)


async def run(factory, origin_url, task, agent, backend, *, github=None, **kwargs) -> tuple[RunResult, FakeGithubClient]:
    github = github if github is not None else FakeGithubClient()
    result = await run_task(
        task.id,
        factory,
        github,
        backend,
        agent=agent,
        workspace_factory=local_workspace_factory(origin_url),
        embedder_warmup=lambda: None,
        **kwargs,
    )
    return result, github


async def spend(factory, task_id, cost: str, model: str = "claude-sonnet-5-5") -> None:
    """One recorded model call, through the gateway's own recorder."""
    await Recorder(factory, Redactor()).record(
        CallRecord(task_id=task_id, stage="agent", model=model, provider="anthropic", cost_usd=Decimal(cost))
    )


def agent_that(stop: StopReason, *, scored: bool = True, spent: tuple | None = None, summary: str | None = "Fixed it."):
    """A scripted agent: optionally spend, optionally edit and score one attempt, then stop for `stop`."""

    async def script(deps):
        if spent is not None:
            await spend(*spent)
        record = None
        if scored:
            edit(deps, "src/app.py", EDITED_APP)
            record = await deps.verify_attempt(1)
        return agent_result(stop, record, summary=summary)

    return ScriptedAgent(script)


def sha_of(repo, ref: str) -> str:
    return git(repo, "rev-parse", ref)


class TestACompletedTaskWithNoPr:
    async def test_every_column_is_written_for_a_product_task_whose_agent_did_not_submit(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        agent = agent_that(StopReason.STEP_CAP, spent=(db_session_factory, task.id, "0.5"))

        result, github = await run(db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]))

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.COMPLETED
        assert row.error_message is None
        assert github.pull_requests == [] and row.pr_number is None and row.pr_url is None
        assert row.changed_files == ["src/app.py"]
        assert "edited by the agent" in row.patch_diff
        assert re.fullmatch(r"[0-9a-f]{40}", row.patch_sha)
        assert row.agent_stop_reason == "step_cap"
        assert row.retry_count == 0
        assert row.cost_usd == Decimal("0.5")
        assert row.score_reason and "nothing was failing" in row.score_reason
        assert row.outcome is None, "nothing was red at the base commit, so it cannot be scored: not `failed`"
        assert row.completed_at is not None
        assert [r.attempt for r in runs] == [0, 1]

    async def test_the_result_carries_what_was_written(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        agent = agent_that(StopReason.STEP_CAP, spent=(db_session_factory, task.id, "0.25"))

        result, _ = await run(db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]))

        assert result.cost_usd == Decimal("0.25")
        assert (result.attempts, result.submitted, result.stop_reason) == (1, False, "step_cap")
        assert "did not submit" in result.pr_gate_reason
        assert result.outcome is None and result.score_reason

    async def test_the_patch_sha_is_the_scored_commit(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        agent = agent_that(StopReason.STEP_CAP)

        await run(db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]))

        row, runs = await reload(db_session_factory, task.id)
        assert row.patch_sha == runs[1].commit_sha, "the sha on the task is the one the suite ran against"

    async def test_a_task_that_called_no_model_has_no_cost_not_a_zero(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)

        result, _ = await run(
            db_session_factory, origin_url, task, agent_that(StopReason.STEP_CAP), FakeBackend(results=[BASELINE, AFTER])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert row.cost_usd is None and result.cost_usd is None

    async def test_two_scored_attempts_are_one_retry(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)

        async def script(deps):
            edit(deps, "src/first.py", "FIRST = 1\n")
            await deps.verify_attempt(1)
            edit(deps, "src/second.py", "SECOND = 1\n")
            record = await deps.verify_attempt(2)
            return agent_result(StopReason.SUBMITTED, record, attempts=2)

        backend = FakeBackend(results=[BASELINE, SuiteResult(passed=()), AFTER])
        result, _ = await run(db_session_factory, origin_url, task, ScriptedAgent(script), backend)

        row, runs = await reload(db_session_factory, task.id)
        assert row.retry_count == 1
        assert [r.attempt for r in runs] == [0, 1, 2]
        assert sorted(row.changed_files) == ["src/first.py", "src/second.py"], "attempt 2 built on attempt 1"
        assert result.attempts == 2

    async def test_an_agent_is_given_the_gateways_task_scope_and_budget(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        budget = TaskBudget(max_usd=1)
        seen = []

        async def script(deps):
            scope = current_scope()
            seen.append((scope.task_id, scope.budget))
            return agent_result(StopReason.NO_CHANGE)

        await run(db_session_factory, origin_url, task, ScriptedAgent(script), FakeBackend(results=[BASELINE]), budget=budget)

        assert seen == [(task.id, budget)]

    async def test_a_second_run_of_a_finished_task_is_not_claimable(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        await run(db_session_factory, origin_url, task, agent_that(StopReason.STEP_CAP), FakeBackend(results=[BASELINE, AFTER]))

        with pytest.raises(TaskNotClaimable):
            await run(db_session_factory, origin_url, task, agent_that(StopReason.STEP_CAP), FakeBackend())


class TestAPrIsOpened:
    async def test_a_submitted_clean_change_opens_a_pr_whose_branch_is_on_the_remote(
        self, db_session, db_session_factory, origin_url, origin, source_repo
    ):
        task = await seed_task(db_session)
        agent = agent_that(StopReason.SUBMITTED, spent=(db_session_factory, task.id, "0.4"), summary="Return {} for an empty file.")

        result, github = await run(db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]))

        row, _ = await reload(db_session_factory, task.id)
        branch = agent_branch_name(7, task.id)
        assert result.status is row.status is TaskStatus.PR_OPENED
        assert row.error_message is None
        assert (row.pr_number, row.pr_url) == (1, "https://github.test/acme/sample/pull/1")
        assert sha_of(origin, f"refs/heads/{branch}") == row.patch_sha, "the branch really reached the remote"
        assert git(origin, "rev-list", "--count", f"main..{branch}") == "1", "squashed to one commit"
        assert result.pr_gate_reason and "submitted" in result.pr_gate_reason

        (pull,) = github.pull_requests
        assert (pull["head"], pull["base"], pull["owner"], pull["repo"]) == (branch, "main", "acme", "sample")
        assert pull["title"] == "[repolace] parse_config crashes on an empty config file"
        assert "Return {} for an empty file." in pull["body"]
        assert "| model | `claude-sonnet-5-5` |" in pull["body"]
        assert "| cost | $0.4000 |" in pull["body"]
        assert "Refs #7" in pull["body"]
        assert not re.search(r"(?i)(fixes|closes|resolves)\s+#", pull["body"])

    async def test_the_squashed_commit_message_carries_no_summary_and_no_outcome(
        self, db_session, db_session_factory, origin_url, origin
    ):
        task = await seed_task(db_session)
        agent = agent_that(StopReason.SUBMITTED, summary="DEFINITELY FIXED, merge now")

        await run(db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]))

        message = git(origin, "log", "-1", "--format=%B", agent_branch_name(7, task.id))
        assert message.splitlines()[0] == "repolace: parse_config crashes on an empty config file"
        assert "Refs #7" in message
        assert "DEFINITELY" not in message

    async def test_a_submitted_change_that_broke_a_test_gets_no_pr(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        broke = SuiteResult(passed=(), failed=(VISIBLE_TEST,), collected_files=("tests/test_app.py",))

        result, github = await run(
            db_session_factory, origin_url, task, agent_that(StopReason.SUBMITTED), FakeBackend(results=[BASELINE, broke])
        )

        assert result.status is TaskStatus.COMPLETED and github.pull_requests == []
        assert "regression" in result.pr_gate_reason

    async def test_the_failure_flag_opens_a_pr_for_a_change_that_broke_a_test(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session, open_pr_on_failure=True)
        broke = SuiteResult(passed=(), failed=(VISIBLE_TEST,), collected_files=("tests/test_app.py",))

        result, github = await run(
            db_session_factory, origin_url, task, agent_that(StopReason.SUBMITTED), FakeBackend(results=[BASELINE, broke])
        )

        assert result.status is TaskStatus.PR_OPENED
        assert "repolace's own checks flagged this change" in github.pull_requests[0]["body"]

    async def test_switching_pull_requests_off_withholds_one_the_gate_would_open(self, db_session, db_session_factory, origin_url, origin):
        task = await seed_task(db_session)

        result, github = await run(
            db_session_factory, origin_url, task, agent_that(StopReason.SUBMITTED),
            FakeBackend(results=[BASELINE, AFTER]), open_pr=False,
        )

        assert result.status is TaskStatus.COMPLETED and github.pull_requests == []
        assert "switched off" in result.pr_gate_reason
        assert "refs/heads/" + agent_branch_name(7, task.id) not in git(origin, "for-each-ref", "--format=%(refname)")


class TestPlumbingStub:
    """`--agent stub`: the default runner, kept flowing through the same code as the real agent."""

    async def test_the_stub_opens_its_not_a_fix_pr_when_the_task_opted_in(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session, open_pr_on_failure=True)

        result, github = await run(db_session_factory, origin_url, task, None, FakeBackend(results=[BASELINE, AFTER]))

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.PR_OPENED
        (pull,) = github.pull_requests
        assert pull["title"] == "[repolace] plumbing smoke test for issue #7"
        assert "NOT A FIX" in pull["body"] and "Please close this pull request." in pull["body"]
        assert "repolace: NOT A FIX" in row.patch_diff, "the marker is in the diff, written above the top retrieval hit"
        assert row.cost_usd is None, "no model ran"

    async def test_the_stub_opens_nothing_without_the_opt_in(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)

        result, github = await run(db_session_factory, origin_url, task, None, FakeBackend(results=[BASELINE, AFTER]))

        assert result.status is TaskStatus.COMPLETED and github.pull_requests == []
        assert result.submitted is False


class TestBenchmarkMode:
    @pytest.fixture
    def instances(self, tmp_path, source_repo):
        directory = tmp_path / "instances"
        directory.mkdir()
        instance = make_instance(base_commit=sha_of(source_repo, "HEAD"))
        dump_instance(instance, directory / f"{instance.instance_id}.json")
        return directory, instance

    async def seed(self, db_session, **overrides):
        return await seed_task(
            db_session, instance_id="acme__sample-7", eval_run_id="run-1", run_index=0, **overrides
        )

    async def test_a_curated_task_scores_passed_and_opens_a_pr_with_no_reference_in_it(
        self, db_session, db_session_factory, origin_url, origin, instances
    ):
        directory, instance = instances
        task = await self.seed(db_session)
        agent = agent_that(StopReason.SUBMITTED, summary="Return {} for an empty file.")

        result, github = await run(
            db_session_factory, origin_url, task, agent,
            FakeBackend(results=[BENCH_BASELINE, BENCH_AFTER]), instances_dir=directory,
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PR_OPENED
        assert row.outcome is TaskOutcome.PASSED
        (pull,) = github.pull_requests
        for text_ in (pull["title"], pull["body"], git(origin, "log", "-1", "--format=%B", pull["head"])):
            assert not re.search(r"#\d", text_), text_
            assert "Refs" not in text_ and "http" not in text_, text_
        assert pull["title"] == "[repolace] SWE-bench instance acme__sample-7"

    async def test_a_curated_list_changes_the_reason_text(self, db_session, db_session_factory, origin_url, instances):
        """With the instance's fail-to-pass list the PASSED reason says how many expected tests pass; without it the
        same results read 'uncurated'. The two sentences are what the report flags the headline by."""
        directory, _ = instances
        task = await self.seed(db_session)

        await run(
            db_session_factory, origin_url, task, agent_that(StopReason.SUBMITTED),
            FakeBackend(results=[BENCH_BASELINE, BENCH_AFTER]), instances_dir=directory,
        )

        row, _ = await reload(db_session_factory, task.id)
        assert row.score_reason == "all 1 expected tests pass, no regressions"
        assert "uncurated" not in row.score_reason

    async def test_the_outcome_comes_from_the_last_scored_attempt_whether_or_not_the_agent_submitted(
        self, db_session, db_session_factory, origin_url, instances
    ):
        """An agent that ran out of steps with a passing last attempt scored PASSED. Only whether to open a
        PR may depend on how it stopped, and in benchmark mode the gate reads the score, not the stop."""
        directory, _ = instances
        task = await self.seed(db_session)

        result, github = await run(
            db_session_factory, origin_url, task, agent_that(StopReason.STEP_CAP),
            FakeBackend(results=[BENCH_BASELINE, BENCH_AFTER]), instances_dir=directory,
        )

        row, _ = await reload(db_session_factory, task.id)
        assert row.outcome is TaskOutcome.PASSED and result.outcome is TaskOutcome.PASSED
        assert row.agent_stop_reason == "step_cap"
        assert result.status is TaskStatus.PR_OPENED and len(github.pull_requests) == 1

    async def test_the_same_results_without_an_instance_read_uncurated(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)

        await run(
            db_session_factory, origin_url, task, agent_that(StopReason.SUBMITTED),
            FakeBackend(results=[BENCH_BASELINE, BENCH_AFTER]),
        )

        row, _ = await reload(db_session_factory, task.id)
        assert row.outcome is TaskOutcome.PASSED
        assert "uncurated" in row.score_reason

    async def test_the_overlay_reaches_every_scored_export_and_never_the_checkout_or_the_diff(
        self, db_session, db_session_factory, origin_url, origin, instances
    ):
        directory, instance = instances
        task = await self.seed(db_session)
        agent = agent_that(StopReason.SUBMITTED)
        backend = FakeBackend(results=[BENCH_BASELINE, BENCH_AFTER])

        await run(db_session_factory, origin_url, task, agent, backend, instances_dir=directory)

        overlay = instance.overlay_bytes()["tests/test_hidden.py"]
        assert backend.runs[0]["snapshot"]["tests/test_hidden.py"] == overlay, "the baseline carries the overlay"
        assert backend.runs[1]["snapshot"]["tests/test_hidden.py"] == overlay, "so does every attempt"
        row, _ = await reload(db_session_factory, task.id)
        assert row.changed_files == ["src/app.py"]
        assert "test_hidden" not in row.patch_diff
        branch_files = git(origin, "ls-tree", "-r", "--name-only", agent_branch_name(7, task.id)).splitlines()
        assert "tests/test_hidden.py" not in branch_files, "the overlay is never pushed"

    async def test_the_agent_is_told_it_is_in_benchmark_mode_and_what_is_hidden(
        self, db_session, db_session_factory, origin_url, instances
    ):
        directory, instance = instances
        task = await self.seed(db_session, issue_body="It raises ValueError.")
        agent = agent_that(StopReason.SUBMITTED)
        backend = FakeBackend(results=[BENCH_BASELINE, BENCH_AFTER])

        await run(db_session_factory, origin_url, task, agent, backend, instances_dir=directory)

        (deps,) = agent.calls
        assert deps.issue.instance_id == "acme__sample-7", "this is what fails the feedback filter closed"
        assert deps.hidden_paths == frozenset({"tests/test_hidden.py"})
        assert deps.issue.body == "It raises ValueError."
        assert deps.baseline == BENCH_BASELINE, "the unfiltered baseline, for the feedback filter alone"
        assert "src/" in deps.repo_overview and "tests/" in deps.repo_overview
        assert "test_hidden" not in deps.repo_overview
        assert all(hit.snippet for hit in deps.retrieved) and len(deps.retrieved) >= 1
        assert deps.tools is None and deps.llm is None, "a runner with no model gets no tools"

    async def test_a_curated_test_that_was_not_red_at_baseline_makes_the_instance_inadmissible_without_running_the_agent(
        self, db_session, db_session_factory, origin_url, instances
    ):
        directory, _ = instances
        task = await self.seed(db_session, open_pr_on_failure=True)
        agent = agent_that(StopReason.SUBMITTED)
        already_green = SuiteResult(
            passed=(VISIBLE_TEST, HIDDEN_TEST), collected_files=("tests/test_app.py", "tests/test_hidden.py")
        )

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[already_green]), instances_dir=directory
        )

        row, runs = await reload(db_session_factory, task.id)
        assert agent.calls == [], "an inadmissible instance must not spend an agent's budget"
        assert result.status is row.status is TaskStatus.COMPLETED
        assert row.outcome is None and result.outcome is None
        assert row.score_reason == f"expected fail-to-pass not red at baseline: {HIDDEN_TEST}"
        assert github.pull_requests == []
        assert (row.agent_stop_reason, row.patch_diff, row.changed_files, row.patch_sha) == (None, None, None, None)
        assert [r.attempt for r in runs] == [0]

    async def test_the_gold_runner_scores_passed_through_the_real_pipeline_and_pushes_nothing(
        self, db_session, db_session_factory, origin_url, origin, instances
    ):
        directory, instance = instances
        task = await self.seed(db_session)

        result, github = await run(
            db_session_factory, origin_url, task, GoldAgent(instance),
            FakeBackend(results=[BENCH_BASELINE, BENCH_AFTER]), instances_dir=directory, open_pr=False,
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.COMPLETED and row.outcome is TaskOutcome.PASSED
        assert github.pull_requests == []
        assert row.changed_files == ["src/app.py"]
        assert row.agent_stop_reason == "submitted"
        assert row.cost_usd is None, "the gold runner calls no model"
        assert agent_branch_name(7, task.id) not in git(origin, "for-each-ref", "--format=%(refname)")

    async def test_the_remote_refs_of_earlier_runs_are_pruned_before_the_agent_runs(
        self, db_session, db_session_factory, origin_url, origin, instances
    ):
        directory, _ = instances
        task = await self.seed(db_session)
        git(origin, "branch", "repolace/issue-7-earlier-run", "main")
        seen = []

        async def script(deps):
            seen.append(git(deps.checkout, "for-each-ref", "--format=%(refname)", "refs/remotes"))
            return agent_result(StopReason.NO_CHANGE)

        await run(
            db_session_factory, origin_url, task, ScriptedAgent(script),
            FakeBackend(results=[BENCH_BASELINE]), instances_dir=directory,
        )

        assert "earlier-run" not in seen[0]
        assert "refs/remotes/origin/main" in seen[0].splitlines()

    async def test_a_task_that_names_an_instance_with_no_directory_supplied_fails_with_the_reason(
        self, db_session, db_session_factory, origin_url
    ):
        task = await self.seed(db_session)

        result, _ = await run(db_session_factory, origin_url, task, agent_that(StopReason.SUBMITTED), FakeBackend())

        assert result.status is TaskStatus.FAILED
        assert result.error_message.startswith("instance: ") and "no instances directory" in result.error_message

    async def test_an_instance_id_that_is_a_path_is_refused_before_anything_is_read(
        self, db_session, db_session_factory, origin_url, tmp_path
    ):
        secret = tmp_path / "secret.json"
        secret.write_text("{}")
        task = await seed_task(db_session, instance_id="../secret", eval_run_id="run-1", run_index=0)
        (tmp_path / "instances").mkdir()

        result, _ = await run(
            db_session_factory, origin_url, task, agent_that(StopReason.SUBMITTED), FakeBackend(),
            instances_dir=tmp_path / "instances",
        )

        assert result.status is TaskStatus.FAILED
        assert result.error_message.startswith("instance: InstanceError")


def tool_call(name: str, **arguments) -> FakeToolCall:
    return FakeToolCall(name=name, arguments=arguments)


class TestTheToolsAreWiredToTheBenchmarkOverlay:
    """The two controls that keep the answer key from the agent through its own tools, pinned at the wiring.

    `build_tool_context` filters a probe by the overlay's paths and builds the write guard from the baseline
    *minus* them. Both depend on `run_task` handing it `verifier.hidden_paths` and the baseline, and both fail
    OPEN if it does not (nothing is hidden, so nothing is filtered; the guard then knows the hidden file and
    refuses it, which says it exists). The unit tests cover the functions; only this covers the call.
    """

    HIDDEN_ID = "checks/check_hidden.py::test_x"
    COLLECTED = ("tests/test_app.py", "checks/check_visible.py", "checks/check_hidden.py")

    @pytest.fixture
    def instances(self, tmp_path, source_repo):
        directory = tmp_path / "instances"
        directory.mkdir()
        instance = make_instance(
            base_commit=sha_of(source_repo, "HEAD"),
            test_files={"checks/check_hidden.py": "def test_x():\n    assert True\n"},
            fail_to_pass=(self.HIDDEN_ID,),
        )
        dump_instance(instance, directory / f"{instance.instance_id}.json")
        return directory

    @pytest.fixture
    async def toolbox_run(self, db_session, db_session_factory, origin_url, instances):
        task = await seed_task(db_session, instance_id="acme__sample-7", eval_run_id="run-1", run_index=0)
        baseline = SuiteResult(passed=(VISIBLE_TEST,), failed=(self.HIDDEN_ID,), collected_files=self.COLLECTED)
        probe = SuiteResult(
            passed=(VISIBLE_TEST,), failed=(self.HIDDEN_ID,), collected_files=self.COLLECTED, exit_code=1
        )
        seen = {}

        async def script(deps):
            seen["probe"] = await deps.tools.dispatch(tool_call("run_tests", targets=["tests/test_app.py"]))
            seen["visible"] = await deps.tools.dispatch(
                tool_call("create_file", path="checks/check_visible.py", content="X = 1\n")
            )
            seen["hidden"] = await deps.tools.dispatch(
                tool_call("create_file", path="checks/check_hidden.py", content="X = 1\n")
            )
            return agent_result(StopReason.NO_CHANGE, None)

        await run(
            db_session_factory, origin_url, task, ScriptedAgent(script),
            FakeBackend(results=[baseline, probe]), instances_dir=instances, llm=NeverCalledLLM(),
        )
        return seen

    async def test_a_probe_that_reports_a_hidden_test_comes_back_without_it(self, toolbox_run):
        probe = toolbox_run["probe"]

        assert not probe.is_error, probe.content
        assert "1 passed, 0 failed" in probe.content, "the hidden failure is not counted either"
        assert "check_hidden" not in probe.content and "test_x" not in probe.content

    async def test_a_baseline_collected_visible_test_file_is_protected(self, toolbox_run):
        refused = toolbox_run["visible"]

        assert refused.is_error and "read-only" in refused.content

    async def test_a_hidden_collected_file_is_not_refused_so_a_refusal_cannot_say_it_exists(self, toolbox_run):
        written = toolbox_run["hidden"]

        assert not written.is_error, written.content


class TestAnUnusableBaseline:
    async def test_the_agent_is_not_run_and_the_task_completes_with_no_outcome(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session, open_pr_on_failure=True)
        agent = agent_that(StopReason.SUBMITTED)
        backend = FakeBackend(results=[SuiteResult(error="verify: pytest exited 2 (INTERRUPTED)")])

        result, github = await run(db_session_factory, origin_url, task, agent, backend)

        row, runs = await reload(db_session_factory, task.id)
        assert agent.calls == [], "an unbuildable instance must not burn an agent's budget"
        assert result.status is row.status is TaskStatus.COMPLETED
        assert row.outcome is None and result.outcome is None
        assert result.pr_gate_reason == "baseline unscoreable"
        assert row.score_reason.startswith("baseline unscoreable: ")
        assert (row.agent_stop_reason, row.patch_diff, row.changed_files, row.patch_sha, row.cost_usd) == (None,) * 5
        assert row.retry_count == 0 and row.error_message is None
        assert github.pull_requests == []
        assert [r.attempt for r in runs] == [0], "the unusable baseline is still recorded"

    async def test_an_environment_that_would_not_build_is_the_same_thing(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        agent = agent_that(StopReason.SUBMITTED)
        backend = FakeBackend(prepare_error=EnvironmentBuildFailed("acme/sample", 1, "pip exploded"))

        result, _ = await run(db_session_factory, origin_url, task, agent, backend)

        row, _ = await reload(db_session_factory, task.id)
        assert agent.calls == []
        assert result.status is TaskStatus.COMPLETED and row.outcome is None
        assert "environment build failed" in row.score_reason


#: Every way an agent can stop that is not "submitted", and not repolace's fault.
AGENT_STOPS = [s for s in StopReason if s is not StopReason.SUBMITTED]


class TestNoAgentCausedStopFailsTheTask:
    """`failed` means repolace broke, and the report removes a failed task from the headline's denominator.
    A stop the agent caused that raised `StageFailed` would therefore quietly improve the number."""

    @pytest.mark.parametrize("stop", AGENT_STOPS, ids=lambda s: s.value)
    async def test_a_stop_with_no_scored_attempt_completes_as_failed_even_with_the_failure_flag(
        self, db_session, db_session_factory, origin_url, stop
    ):
        task = await seed_task(db_session, open_pr_on_failure=True)
        agent = agent_that(stop, scored=False)

        result, github = await run(db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE]))

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.COMPLETED
        assert row.error_message is None
        assert row.outcome is TaskOutcome.FAILED, "no scored attempt scores failed; it is the agent's"
        expected = "agent produced no change" if stop is StopReason.NO_CHANGE else f"no scored attempt: {stop.value}"
        assert row.score_reason == expected
        assert row.agent_stop_reason == stop.value
        assert (row.patch_diff, row.changed_files) == ("", [])
        assert github.pull_requests == [], "an empty diff is never pushed, not even for `open_pr_on_failure`"
        assert "no change" in result.pr_gate_reason
        assert [r.attempt for r in runs] == [0]

    @pytest.mark.parametrize("stop", AGENT_STOPS, ids=lambda s: s.value)
    async def test_a_stop_after_a_scored_attempt_completes_with_the_scorers_outcome(
        self, db_session, db_session_factory, origin_url, stop
    ):
        task = await seed_task(db_session)
        agent = agent_that(stop)

        result, github = await run(db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]))

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.COMPLETED
        assert row.error_message is None
        assert row.agent_stop_reason == stop.value
        assert row.changed_files == ["src/app.py"]
        assert github.pull_requests == [], "an agent that did not submit gets no PR without the opt-in"

    @pytest.mark.parametrize("stop", AGENT_STOPS, ids=lambda s: s.value)
    async def test_a_stop_after_a_scored_attempt_with_the_failure_flag_opens_a_pr_and_still_completes_the_row(
        self, db_session, db_session_factory, origin_url, stop
    ):
        task = await seed_task(db_session, open_pr_on_failure=True)

        result, github = await run(
            db_session_factory, origin_url, task, agent_that(stop), FakeBackend(results=[BASELINE, AFTER])
        )

        assert result.status is TaskStatus.PR_OPENED and len(github.pull_requests) == 1
        assert result.error_message is None

    async def test_a_submit_with_an_empty_diff_and_the_failure_flag_does_not_become_a_harness_error(
        self, db_session, db_session_factory, origin_url, origin
    ):
        """The squash path: the gate must refuse before `squash` ever sees a branch with no net change."""
        task = await seed_task(db_session, open_pr_on_failure=True)

        async def script(deps):
            edit(deps, "src/app.py", EDITED_APP)
            await deps.verify_attempt(1)
            edit(deps, "src/app.py", APP_SOURCE)  # back to the base content
            return agent_result(StopReason.SUBMITTED, None, attempts=0)

        result, github = await run(
            db_session_factory, origin_url, task, ScriptedAgent(script), FakeBackend(results=[BASELINE, AFTER])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.COMPLETED and row.error_message is None
        assert github.pull_requests == []

    async def test_a_squash_that_finds_no_net_change_is_no_pr_not_a_harness_error(
        self, db_session, db_session_factory, origin_url
    ):
        """The gate saw a change and the squash saw none. Reachable only by reading "change" twice and
        getting two answers, and never the agent's doing -- but failing the task would be wrong the other way."""
        task = await seed_task(db_session, open_pr_on_failure=True)

        @asynccontextmanager
        async def workspace_whose_squash_finds_nothing(*args, **kwargs):
            async with task_workspace(*args, clone_url=origin_url, **kwargs) as workspace:
                async def nothing(message):
                    return None

                workspace.squash = nothing
                yield workspace

        github = FakeGithubClient()
        result = await run_task(
            task.id, db_session_factory, github, FakeBackend(results=[BASELINE, AFTER]),
            agent=agent_that(StopReason.SUBMITTED),
            workspace_factory=workspace_whose_squash_finds_nothing,
            embedder_warmup=lambda: None,
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.COMPLETED and row.error_message is None
        assert github.pull_requests == []
        assert "no net change" in result.pr_gate_reason
        assert row.changed_files == ["src/app.py"], "the change the agent made is still recorded"

    async def test_an_agent_that_lets_a_budget_exception_escape_still_completes(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)

        async def script(deps):
            edit(deps, "src/app.py", EDITED_APP)
            await deps.verify_attempt(1)
            raise BudgetExceeded(BudgetLimit.USD, spent_usd=Decimal("2.5"), calls=9, elapsed_seconds=3.0)

        result, _ = await run(
            db_session_factory, origin_url, task, ScriptedAgent(script), FakeBackend(results=[BASELINE, AFTER])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.COMPLETED and row.error_message is None
        assert row.agent_stop_reason == "budget_usd"
        assert row.changed_files == ["src/app.py"], "the scored attempt before the stop is kept"
        assert row.retry_count == 0 and result.attempts == 1

    async def test_an_agent_that_lets_an_llm_error_escape_still_completes_with_no_scored_attempt(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)

        async def script(deps):
            raise LLMCallError("provider down", stage="agent", model="m", retries=3)

        result, _ = await run(db_session_factory, origin_url, task, ScriptedAgent(script), FakeBackend(results=[BASELINE]))

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.COMPLETED and row.error_message is None
        assert row.agent_stop_reason == "llm_error"
        assert row.outcome is TaskOutcome.FAILED and row.score_reason == "no scored attempt: llm_error"


class TestWhatDoesFailTheTask:
    async def test_an_unpriced_model_fails_the_task_and_still_shows_the_spend(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)

        async def script(deps):
            await spend(db_session_factory, task.id, "0.25")
            raise UnpricedModelError("no price for some/model")

        result, _ = await run(db_session_factory, origin_url, task, ScriptedAgent(script), FakeBackend(results=[BASELINE]))

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.FAILED
        assert row.error_message.startswith("agent: UnpricedModelError")
        assert row.cost_usd == Decimal("0.25"), "a task that fails after spending still shows the spend"
        assert result.cost_usd == Decimal("0.25")
        assert row.outcome is None, "a failure is repolace's and is never scored"
        assert row.completed_at is not None

    async def test_a_failure_before_any_model_call_has_no_cost(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        github = FakeGithubClient(permissions={})

        result, _ = await run(
            db_session_factory, origin_url, task, agent_that(StopReason.SUBMITTED), FakeBackend(), github=github
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.FAILED and row.error_message.startswith("preflight: ")
        assert row.cost_usd is None
        assert (row.agent_stop_reason, row.patch_diff, row.changed_files, row.patch_sha) == (None, None, None, None)

    async def test_a_failure_after_the_agent_records_what_the_agent_had_produced(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        agent = agent_that(StopReason.SUBMITTED, spent=(db_session_factory, task.id, "0.75"))
        github = FakeGithubClient(pull_request_error=RuntimeError("GitHub is down"))

        result, _ = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]), github=github
        )

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.FAILED
        assert row.error_message.startswith("pr: RuntimeError: GitHub is down")
        assert row.cost_usd == Decimal("0.75")
        assert row.agent_stop_reason == "submitted"
        assert row.changed_files == ["src/app.py"]
        assert "edited by the agent" in row.patch_diff
        assert re.fullmatch(r"[0-9a-f]{40}", row.patch_sha)
        assert row.patch_sha != runs[1].commit_sha, "the squashed commit, which is what was pushed"
        assert row.outcome is None and row.pr_number is None
        assert (result.stop_reason, result.attempts, result.submitted) == ("submitted", 1, True)

    async def test_an_agent_that_returns_something_else_fails_loudly(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)

        async def script(deps):
            return "done"

        result, _ = await run(db_session_factory, origin_url, task, ScriptedAgent(script), FakeBackend(results=[BASELINE]))

        assert result.status is TaskStatus.FAILED
        assert result.error_message.startswith("agent: TypeError") and "AgentResult" in result.error_message


class TestRewindToTheLastScoredCommit:
    """`last_attempt` is the only scored state. These drive the real tools, whose probes leave unscored checkpoint commits."""

    @staticmethod
    def call(name: str, **arguments):
        return FakeToolCall(name=name, arguments=arguments)

    async def test_unscored_checkpoint_edits_never_reach_the_diff_the_sha_or_the_pr(
        self, db_session, db_session_factory, origin_url, origin
    ):
        task = await seed_task(db_session, open_pr_on_failure=True)

        async def script(deps):
            ok = await deps.tools.dispatch(self.call("create_file", path="src/scored.py", content="SCORED = 1\n"))
            assert not ok.is_error, ok.content
            record = await deps.verify_attempt(1)
            ok = await deps.tools.dispatch(self.call("create_file", path="src/unscored.py", content="UNSCORED = 1\n"))
            assert not ok.is_error, ok.content
            # A probe commits the tree first, so the unscored edit becomes a commit ahead of the scored one.
            probe = await deps.tools.dispatch(self.call("run_python", code="print(1)"))
            assert not probe.is_error, probe.content
            return agent_result(StopReason.BUDGET_USD, record, summary=None)

        backend = FakeBackend(results=[BASELINE, AFTER], scripts=[ScriptResult(exit_code=0, stdout="1\n")])
        result, github = await run(
            db_session_factory, origin_url, task, ScriptedAgent(script), backend, llm=NeverCalledLLM()
        )

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.PR_OPENED
        assert row.changed_files == ["src/scored.py"], "the unscored edit is not part of the scored state"
        assert "UNSCORED" not in row.patch_diff and "SCORED = 1" in row.patch_diff
        assert "src/unscored.py" not in git(origin, "ls-tree", "-r", "--name-only", agent_branch_name(7, task.id))
        assert "unscored" not in github.pull_requests[0]["body"]
        assert len(backend.scripts) == 1, "the probe did run, in the sandbox"

    async def test_with_no_scored_attempt_the_tree_is_rewound_to_the_base(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session, open_pr_on_failure=True)

        async def script(deps):
            await deps.tools.dispatch(self.call("create_file", path="src/unscored.py", content="UNSCORED = 1\n"))
            await deps.tools.dispatch(self.call("run_python", code="print(1)"))
            return agent_result(StopReason.STEP_CAP, None, summary=None)

        backend = FakeBackend(results=[BASELINE], scripts=[ScriptResult(exit_code=0)])
        result, github = await run(
            db_session_factory, origin_url, task, ScriptedAgent(script), backend, llm=NeverCalledLLM()
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.COMPLETED and row.outcome is TaskOutcome.FAILED
        assert (row.patch_diff, row.changed_files) == ("", [])
        assert github.pull_requests == []


class TestIndexContention:
    """The lock that makes indexing single-flight is a try-lock, so a second task on one repo is refused at once."""

    async def test_a_task_waits_for_the_lock_instead_of_failing(
        self, db_session, db_session_factory, db_engine, origin_url, monkeypatch
    ):
        task = await seed_task(db_session)
        holder = await db_engine.connect()
        await holder.execute(text("select pg_advisory_xact_lock(:key)"), {"key": _repo_lock_key(task.repo_id)})
        sleeps: list[float] = []

        async def release_on_first_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            await holder.rollback()

        monkeypatch.setattr(run_module, "_sleep", release_on_first_sleep)
        try:
            result, _ = await run(
                db_session_factory, origin_url, task, agent_that(StopReason.STEP_CAP), FakeBackend(results=[BASELINE, AFTER])
            )
        finally:
            await holder.close()

        assert sleeps == [run_module.INDEX_WAIT_STEP_SECONDS], "it waited one step, then the lock was free"
        assert result.status is TaskStatus.COMPLETED and result.error_message is None

    async def test_it_gives_up_after_the_cap_with_a_message_that_says_why(
        self, db_session, db_session_factory, db_engine, origin_url, monkeypatch
    ):
        task = await seed_task(db_session)
        holder = await db_engine.connect()
        await holder.execute(text("select pg_advisory_xact_lock(:key)"), {"key": _repo_lock_key(task.repo_id)})
        sleeps: list[float] = []

        async def never_release(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(run_module, "_sleep", never_release)
        monkeypatch.setattr(run_module, "INDEX_WAIT_STEP_SECONDS", 10.0)
        monkeypatch.setattr(run_module, "INDEX_WAIT_CAP_SECONDS", 30.0)
        try:
            result, _ = await run(
                db_session_factory, origin_url, task, agent_that(StopReason.STEP_CAP), FakeBackend()
            )
        finally:
            await holder.close()

        assert sleeps == [10.0, 10.0, 10.0]
        assert result.status is TaskStatus.FAILED
        assert result.error_message.startswith("index: another task held this repo's indexing lock for more than 30s")


class TestRetrievalFeedsTheAgent:
    async def test_the_agent_is_given_the_retrieved_snippets_from_the_clean_tree(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        agent = agent_that(StopReason.NO_CHANGE, scored=False)

        await run(db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE]))

        (deps,) = agent.calls
        assert deps.retrieved, "retrieval found something"
        assert any("parse_config" in hit.snippet for hit in deps.retrieved) or any(hit.snippet for hit in deps.retrieved)
        assert deps.baseline_files == ("README.md", "src/__init__.py", "src/app.py", "tests/test_app.py")
        assert deps.limits.max_attempts == 3

    async def test_a_model_driven_agent_gets_the_toolbox_and_the_search_tool_reaches_the_index(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        seen = {}

        async def script(deps):
            assert deps.tools is not None
            seen["names"] = deps.tools.names
            hits = await deps.tools.dispatch(TestRewindToTheLastScoredCommit.call("search_code", query="parse_config"))
            seen["hits"] = hits
            return agent_result(StopReason.NO_CHANGE, None)

        await run(
            db_session_factory, origin_url, task, ScriptedAgent(script), FakeBackend(results=[BASELINE]), llm=NeverCalledLLM()
        )

        assert seen["names"] == (
            "search_code", "read_file", "grep", "list_dir", "edit_file", "create_file", "run_python", "run_tests", "submit",
        )
        assert not seen["hits"].is_error, seen["hits"].content
        assert "src/app.py" in seen["hits"].content
