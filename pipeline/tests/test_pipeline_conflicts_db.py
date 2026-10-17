"""A target branch that moved while the task ran, through the whole of `run_task`.

The same stack as `test_pipeline_run_task_db.py`: a real database, a real local git remote, the
real merge primitives, scorer, gate, squash and push. Faked are the sandbox (`FakeBackend`),
GitHub (`FakeGithubClient`), the agent (a scripted runner that edits, scores, and then has
"someone else" push to the target branch) and the resolver (a scripted one, since the real one
calls a model). Each test states what the task row says afterwards, because the row is what the
report and the UI read.
"""

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from pipeline_support import (
    AFTER,
    APP_SOURCE,
    BASELINE,
    VISIBLE_TEST,
    FakeGithubClient,
    NeverCalledLLM,
    ScriptedAgent,
    agent_result,
    edit,
    git,
    make_instance,
    reload,
    seed_task,
)
from repolace_agents.contracts import StopReason
from repolace_agents.resolver import ResolverResult, ResolverStop
from repolace_agents.tools.base import ToolError
from repolace_agents.tools.paths import confine
from repolace_gateway.budget import TaskBudget, current_scope
from repolace_gateway.errors import AUTH_FAILURE_PREFIX, KIND_AUTH, LLMCallError
from repolace_pipeline.conflicts import RESOLVER_MAX_USD, RESOLVER_MAX_WALL_SECONDS
from repolace_pipeline.run import run_task
from repolace_shared.db.models import TaskOutcome, TaskStage, TaskStatus
from repolace_shared.git import task_workspace
from repolace_shared.instances import dump_instance
from sqlalchemy import select
from verify.protocol import SuiteResult
from verify.testing import FakeBackend

pytestmark = [pytest.mark.anyio, pytest.mark.db, pytest.mark.usefixtures("embedder")]

OURS_LINE = '    """Parse the key=value config file at path into a dict."""'
#: Our side and the target's change the same line of `src/app.py` differently: a both-modified conflict.
AGENT_APP = APP_SOURCE.replace(OURS_LINE, '    """Parse the key=value config file at `path` into a dict."""')
UPSTREAM_APP = APP_SOURCE.replace(OURS_LINE, '    """Parse a key=value config file into a dict of strings."""')
RESOLVED_APP = APP_SOURCE.replace(OURS_LINE, '    """Parse a key=value config file at `path` into a dict of strings."""')
#: A second file only our side touches, so a resolver handed it would have been handed too much.
AGENT_INIT = "# package marker\n"

HIDDEN_TEST = "tests/test_hidden.py::test_empty_config"
BENCH_COLLECTED = ("tests/test_app.py", "tests/test_hidden.py")
BENCH_BASELINE = SuiteResult(passed=(VISIBLE_TEST,), failed=(HIDDEN_TEST,), collected_files=BENCH_COLLECTED)
BENCH_AFTER = SuiteResult(passed=(VISIBLE_TEST, HIDDEN_TEST), collected_files=BENCH_COLLECTED)
REGRESSED = SuiteResult(failed=(VISIBLE_TEST,), collected_files=("tests/test_app.py",))


# --- the other side of the race ------------------------------------------------


@dataclass
class Upstream:
    """Someone else pushing to the target branch, from a clone of their own."""

    origin: Path
    workdir: Path
    pushed: list[str] = field(default_factory=list)

    def push(self, files: dict[str, str | tuple[str, str] | None], message: str = "upstream change") -> str:
        work = self.workdir / f"upstream-{len(self.pushed)}"
        git(self.workdir, "clone", "-q", "--branch", "main", str(self.origin), str(work))
        for name, content in files.items():
            path = work / name
            if content is None:
                git(work, "rm", "-q", name)
            elif isinstance(content, tuple):
                path.unlink()
                os.symlink(content[1], path)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", message)
        git(work, "push", "-q", "origin", "main")
        sha = git(work, "rev-parse", "HEAD")
        self.pushed.append(sha)
        return sha


@pytest.fixture
def upstream(origin, tmp_path) -> Upstream:
    workdir = tmp_path / "upstream"
    workdir.mkdir()
    return Upstream(origin, workdir)


def racing_agent(upstream: Upstream | None, files: dict | None, *, edits: dict[str, str] | None = None) -> ScriptedAgent:
    """Edit, score attempt 1, then (while the task is still running) have the target branch move."""

    async def script(deps):
        for path, text in (edits or {"src/app.py": AGENT_APP, "src/__init__.py": AGENT_INIT}).items():
            edit(deps, path, text)
        record = await deps.verify_attempt(1)
        if upstream is not None and files is not None:
            upstream.push(files)
        return agent_result(StopReason.SUBMITTED, record, summary="Documented the path argument.")

    return ScriptedAgent(script)


@dataclass
class ScriptedResolver:
    """A resolver that writes what it is told, reporting what the host's check said before and after."""

    stop: ResolverStop = ResolverStop.RESOLVED
    writes: dict[str, str] = field(default_factory=lambda: {"src/app.py": RESOLVED_APP})
    error: BaseException | None = None
    calls: list = field(default_factory=list)
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)
    budgets: list = field(default_factory=list)

    async def __call__(self, deps):
        self.calls.append(deps)
        self.budgets.append(current_scope().budget)
        if self.error is not None:
            raise self.error
        self.before.append(await deps.check(deps.conflicts))
        for path, text in self.writes.items():
            (deps.checkout / path).write_text(text)
        self.after.append(await deps.check(deps.conflicts))
        return ResolverResult(self.stop, "Kept both docstring edits.", rounds=1, steps=2)


def observing_factory(url: str, seen: dict):
    """`task_workspace` on the local remote, noting the checkout's state just before it is deleted."""

    @asynccontextmanager
    async def factory(*args, **kwargs):
        async with task_workspace(*args, clone_url=url, **kwargs) as workspace:
            try:
                yield workspace
            finally:
                seen["head"] = await workspace.repo.head_sha()
                seen["merging"] = await workspace.repo.merge_in_progress()
                seen["base"] = workspace.base_sha

    return factory


async def run(factory, origin_url, task, agent, backend, *, resolver=None, llm="never", seen=None, **kwargs):
    github = FakeGithubClient()
    result = await run_task(
        task.id,
        factory,
        github,
        backend,
        agent=agent,
        llm=NeverCalledLLM() if llm == "never" else llm,
        resolver=resolver if resolver is not None else ScriptedResolver(),
        workspace_factory=observing_factory(origin_url, seen if seen is not None else {}),
        embedder_warmup=lambda: None,
        **kwargs,
    )
    return result, github


async def stage_rows(factory, task_id) -> dict[str, list[str]]:
    async with factory() as session:
        rows = (await session.execute(select(TaskStage).where(TaskStage.task_id == task_id))).scalars().all()
    stages: dict[str, list[str]] = {}
    for row in rows:
        stages.setdefault(row.stage, []).append(row.status)
    return stages


def on_origin(origin: Path, ref: str, path: str) -> str:
    return git(origin, "show", f"{ref}:{path}")


# --- the common path ---------------------------------------------------------


class TestATargetThatDidNotMove:
    async def test_the_step_changes_nothing_and_runs_no_extra_suite(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        resolver = ScriptedResolver()
        backend = FakeBackend(results=[BASELINE, AFTER])

        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(None, None), backend, resolver=resolver
        )

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PR_OPENED
        assert row.conflict_reason is None and result.conflict_reason is None
        assert [r.attempt for r in runs] == [0, 1] and len(backend.runs) == 2
        assert resolver.calls == []
        assert len(github.pull_requests) == 1
        assert (await stage_rows(db_session_factory, task.id))["conflicts"] == ["ok"]

    async def test_a_task_whose_pr_is_withheld_never_fetches_the_target(
        self, db_session, db_session_factory, origin_url, upstream
    ):
        """No PR, nothing to conflict: the step is skipped, so the moved branch is never merged or scored."""
        task = await seed_task(db_session)
        backend = FakeBackend(results=[BASELINE, AFTER])

        await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/extra.py": "X = 1\n"}),
            backend, open_pr=False,
        )

        row, runs = await reload(db_session_factory, task.id)
        assert row.status is TaskStatus.COMPLETED and row.conflict_reason is None
        assert [r.attempt for r in runs] == [0, 1]
        assert "conflicts" not in await stage_rows(db_session_factory, task.id)


# --- a clean merge -------------------------------------------------------------


class TestACleanMerge:
    async def test_the_base_advances_and_the_merged_tree_is_scored_and_proposed(
        self, db_session, db_session_factory, origin_url, origin, upstream
    ):
        task = await seed_task(db_session)
        backend = FakeBackend(results=[BASELINE, AFTER, AFTER])
        seen: dict = {}

        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/extra.py": "X = 1\n"}),
            backend, seen=seen,
        )

        row, runs = await reload(db_session_factory, task.id)
        (tip,) = upstream.pushed
        assert result.status is row.status is TaskStatus.PR_OPENED
        assert [r.attempt for r in runs] == [0, 1, 2], "the pre-merge attempt is kept and the merge is scored"
        assert "src/extra.py" in backend.runs[2]["snapshot"], "the third suite ran on the merged tree"
        assert "src/extra.py" not in backend.runs[1]["snapshot"]
        assert seen["base"] == tip
        (pull,) = github.pull_requests
        assert git(origin, "rev-parse", f"{pull['head']}^") == tip, "the PR is one commit on the new tip"
        assert sorted(row.changed_files) == ["src/__init__.py", "src/app.py"], "only the agent's own change"
        assert "src/extra.py" not in row.patch_diff
        assert row.patch_sha == git(origin, "rev-parse", pull["head"])

    async def test_the_gate_judges_the_merged_attempt_not_the_pre_merge_one(
        self, db_session, db_session_factory, origin_url, upstream
    ):
        """The pre-merge attempt was clean; the merged one regresses. No PR, because the merged tree is the one a PR would carry."""
        task = await seed_task(db_session)
        backend = FakeBackend(results=[BASELINE, AFTER, REGRESSED])

        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/extra.py": "X = 1\n"}), backend
        )

        row, runs = await reload(db_session_factory, task.id)
        assert row.status is TaskStatus.COMPLETED and row.error_message is None and row.conflict_reason is None
        assert github.pull_requests == []
        assert "flagged" in result.pr_gate_reason
        assert row.patch_sha == runs[2].commit_sha, "the scored commit recorded is the merged one"

    async def test_a_target_that_already_holds_the_change_opens_no_pr(
        self, db_session, db_session_factory, origin_url, upstream
    ):
        task = await seed_task(db_session)
        backend = FakeBackend(results=[BASELINE, AFTER])

        result, github = await run(
            db_session_factory, origin_url, task,
            racing_agent(upstream, {"src/extra.py": "X = 1\n"}, edits={"src/extra.py": "X = 1\n"}), backend,
        )

        row, runs = await reload(db_session_factory, task.id)
        assert row.status is TaskStatus.COMPLETED and github.pull_requests == []
        assert [r.attempt for r in runs] == [0, 1], "nothing new to score"
        assert "already contains" in result.pr_gate_reason
        assert row.patch_sha == runs[1].commit_sha


# --- a conflict the resolver settles -----------------------------------------


class TestAResolvedConflict:
    @pytest.fixture
    async def resolved(self, db_session, db_session_factory, origin_url, origin, upstream):
        task = await seed_task(db_session)
        resolver = ScriptedResolver()
        backend = FakeBackend(results=[BASELINE, AFTER, AFTER])
        seen: dict = {}
        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/app.py": UPSTREAM_APP}),
            backend, resolver=resolver, seen=seen, budget=TaskBudget(max_usd=Decimal("2.00")),
        )
        row, runs = await reload(db_session_factory, task.id)
        return result, row, runs, github, resolver, seen, origin, upstream

    async def test_the_resolution_is_committed_scored_and_proposed(self, resolved):
        result, row, runs, github, _, seen, origin, upstream = resolved

        assert result.status is row.status is TaskStatus.PR_OPENED and row.conflict_reason is None
        assert [r.attempt for r in runs] == [0, 1, 2]
        (pull,) = github.pull_requests
        assert on_origin(origin, pull["head"], "src/app.py") == RESOLVED_APP.strip(), "marker-free, as resolved"
        assert git(origin, "rev-parse", f"{pull['head']}^") == upstream.pushed[0] == seen["base"]
        assert seen["merging"] is False

    async def test_the_resolver_got_exactly_the_conflicted_path_and_the_host_checked_it(self, resolved):
        _, _, runs, _, resolver, _, _, upstream = resolved

        (deps,) = resolver.calls
        assert tuple(deps.conflicts) == ("src/app.py",), "src/__init__.py changed on our side only and is not handed over"
        assert deps.base_sha == runs[1].commit_sha and deps.theirs_sha == upstream.pushed[0]
        (before,) = resolver.before
        assert before and "conflict marker" in before[0], "the host's check sees the markers"
        assert resolver.after == [[]]

    async def test_the_resolver_has_the_agents_write_guard_and_cannot_write_git(self, resolved):
        _, _, _, _, resolver, _, _, _ = resolved
        (deps,) = resolver.calls

        assert deps.is_protected("tests/test_app.py"), "a collected test file is read-only, as for the main agent"
        assert not deps.is_protected("src/app.py")
        for path in (".git/config", ".git/hooks/post-commit", ".gitattributes"):
            with pytest.raises(ToolError):
                confine(deps.checkout, path, write=True, is_protected=deps.is_protected)

    async def test_the_resolver_runs_on_a_budget_of_its_own(self, resolved):
        _, _, _, _, resolver, _, _, _ = resolved
        (budget,) = resolver.budgets

        assert budget.max_usd == RESOLVER_MAX_USD, "half of $2.00 is capped at $0.50"
        assert budget.max_wall_seconds == RESOLVER_MAX_WALL_SECONDS
        assert budget.spent_usd == 0 and budget.calls == 0


# --- conflicts that end the task as `conflicting` -----------------------------


class TestAConflictThatCannotBeResolved:
    async def assert_conflicting(self, factory, task_id, result, github, seen, runs, needle: str):
        row, _ = await reload(factory, task_id)
        assert result.status is row.status is TaskStatus.CONFLICTING
        assert row.error_message is None and result.error_message is None, "nothing broke"
        assert needle in row.conflict_reason and row.conflict_reason == result.conflict_reason
        assert "src/" not in row.conflict_reason, "the reason names no path"
        assert github.pull_requests == [] and row.pr_number is None
        assert seen["head"] == runs[1].commit_sha, "abort_merge left the checkout at its pre-merge commit"
        assert seen["merging"] is False
        assert row.patch_sha == runs[1].commit_sha
        return row

    async def test_a_resolver_that_gives_up_keeps_the_pre_merge_outcome(
        self, db_session, db_session_factory, origin_url, upstream, tmp_path, source_repo
    ):
        directory = tmp_path / "instances"
        directory.mkdir()
        instance = make_instance(base_commit=git(source_repo, "rev-parse", "HEAD"))
        dump_instance(instance, directory / f"{instance.instance_id}.json")
        task = await seed_task(db_session, instance_id=instance.instance_id, eval_run_id="run-1", run_index=0)
        backend = FakeBackend(results=[BENCH_BASELINE, BENCH_AFTER])
        seen: dict = {}

        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/app.py": UPSTREAM_APP}), backend,
            resolver=ScriptedResolver(stop=ResolverStop.GAVE_UP, writes={}), seen=seen, instances_dir=directory,
        )

        _, runs = await reload(db_session_factory, task.id)
        row = await self.assert_conflicting(db_session_factory, task.id, result, github, seen, runs, "gave_up")
        assert row.outcome is TaskOutcome.PASSED, "the pre-merge attempt's score stands"
        assert [r.attempt for r in runs] == [0, 1], "nothing merged, nothing more scored"
        assert seen["base"] == instance.base_commit, "the base never advanced"

    @pytest.mark.parametrize(
        "stop",
        [
            ResolverStop.ROUNDS_EXHAUSTED,
            ResolverStop.STEP_CAP,
            ResolverStop.BUDGET,
            ResolverStop.LLM_ERROR,
            ResolverStop.PROTECTED_PATH,
        ],
    )
    async def test_every_other_unresolved_stop_is_conflicting_too(
        self, db_session, db_session_factory, origin_url, upstream, stop
    ):
        task = await seed_task(db_session)
        seen: dict = {}

        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/app.py": UPSTREAM_APP}),
            FakeBackend(results=[BASELINE, AFTER]), resolver=ScriptedResolver(stop=stop, writes={}), seen=seen,
        )

        _, runs = await reload(db_session_factory, task.id)
        await self.assert_conflicting(db_session_factory, task.id, result, github, seen, runs, stop.value)

    async def test_a_delete_modify_conflict_never_reaches_the_resolver(
        self, db_session, db_session_factory, origin_url, upstream
    ):
        task = await seed_task(db_session)
        resolver = ScriptedResolver()
        seen: dict = {}

        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/app.py": None}),
            FakeBackend(results=[BASELINE, AFTER]), resolver=resolver, seen=seen,
        )

        _, runs = await reload(db_session_factory, task.id)
        await self.assert_conflicting(db_session_factory, task.id, result, github, seen, runs, "delete_modify")
        assert resolver.calls == []

    async def test_a_symlink_conflict_never_reaches_the_resolver(
        self, db_session, db_session_factory, origin_url, upstream
    ):
        task = await seed_task(db_session)
        resolver = ScriptedResolver()
        seen: dict = {}

        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/app.py": ("symlink", "../README.md")}),
            FakeBackend(results=[BASELINE, AFTER]), resolver=resolver, seen=seen,
        )

        _, runs = await reload(db_session_factory, task.id)
        await self.assert_conflicting(db_session_factory, task.id, result, github, seen, runs, "symlink")
        assert resolver.calls == []

    async def test_a_resolver_that_edits_outside_the_conflicted_set_is_refused(
        self, db_session, db_session_factory, origin_url, upstream
    ):
        """The tools cannot do this; the host's commit check is what would stop it if they ever could."""
        task = await seed_task(db_session)
        resolver = ScriptedResolver(writes={"src/app.py": RESOLVED_APP, "README.md": "# rewritten\n"})
        seen: dict = {}

        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/app.py": UPSTREAM_APP}),
            FakeBackend(results=[BASELINE, AFTER]), resolver=resolver, seen=seen,
        )

        _, runs = await reload(db_session_factory, task.id)
        await self.assert_conflicting(db_session_factory, task.id, result, github, seen, runs, "refused")

    async def test_a_run_with_no_model_cannot_resolve(self, db_session, db_session_factory, origin_url, upstream):
        task = await seed_task(db_session)
        resolver = ScriptedResolver()
        seen: dict = {}

        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/app.py": UPSTREAM_APP}),
            FakeBackend(results=[BASELINE, AFTER]), resolver=resolver, llm=None, seen=seen,
        )

        _, runs = await reload(db_session_factory, task.id)
        await self.assert_conflicting(db_session_factory, task.id, result, github, seen, runs, "no model")
        assert resolver.calls == []


# --- a provider that refuses us ------------------------------------------------


class TestAProviderRefusalInTheResolver:
    async def test_it_fails_the_task_as_a_harness_error(self, db_session, db_session_factory, origin_url, upstream):
        task = await seed_task(db_session)
        refused = LLMCallError(
            f"{AUTH_FAILURE_PREFIX}the key was rejected", stage="resolve", model="m", retries=0, kind=KIND_AUTH
        )

        result, github = await run(
            db_session_factory, origin_url, task, racing_agent(upstream, {"src/app.py": UPSTREAM_APP}),
            FakeBackend(results=[BASELINE, AFTER]), resolver=ScriptedResolver(error=refused),
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.FAILED
        assert row.error_message.startswith("conflicts: ") and AUTH_FAILURE_PREFIX.strip() in row.error_message
        assert row.conflict_reason is None and github.pull_requests == []
