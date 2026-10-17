"""A target branch that moved while the task ran, end to end through `run_task`.

The remote is a real bare repository and the target moves the way it does in life: the scripted agent
pushes a commit to it after its attempt was scored, so the clone, the fetch, the merge, the resolution,
the squash and the push are all real git. Faked are only the sandbox (`FakeBackend`, which fails the test
on a suite run nobody scripted), GitHub and the model -- and for most tests the resolver, which is a
scripted function here; one test runs the real resolver over a scripted provider.

`CheckoutAtExit` reads the checkout just before the workspace deletes it, which is how "the abort left the
checkout at its pre-merge commit" is observed rather than inferred.
"""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from pipeline_llm_support import ScriptedProvider, call, llm_rows, make_client, reply
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
    push_upstream,
    reload,
    rewrite_upstream,
    seed_task,
)
from repolace_agents.contracts import AgentDeps, StopReason
from repolace_agents.resolver import ResolverDeps, ResolverResult, ResolverStop, resolve_conflicts
from repolace_gateway.budget import BudgetExceeded, BudgetLimit
from repolace_gateway.errors import KIND_AUTH, LLMCallError
from repolace_pipeline.conflicts import ALREADY_CONTAINED_REASON
from repolace_pipeline.run import RunResult, run_task
from repolace_shared.db.models import TaskOutcome, TaskStage, TaskStatus
from repolace_shared.git import task_workspace
from repolace_shared.instances import dump_instance
from sqlalchemy import select
from verify.protocol import SuiteResult
from verify.testing import FakeBackend

pytestmark = [pytest.mark.anyio, pytest.mark.db, pytest.mark.usefixtures("embedder")]

OURS = APP_SOURCE + "\n# edited by the agent\n"
THEIRS = APP_SOURCE + "\n# upstream change\n"
RESOLVED = APP_SOURCE + "\n# edited by the agent\n# upstream change\n"
README_UPSTREAM = "# sample\n\nUpstream documentation.\n"

OTHER = "tests/test_app.py::test_other"
COLLECTED = ("tests/test_app.py",)
#: A live issue with one red test that the attempt turns green: PASSED (uncurated), so a kept
#: pre-merge outcome is visible as something other than NULL.
RED_BASELINE = SuiteResult(passed=(OTHER,), failed=(VISIBLE_TEST,), collected_files=COLLECTED)
GREEN = SuiteResult(passed=(OTHER, VISIBLE_TEST), collected_files=COLLECTED)
#: The merged tree breaks the test that passed at baseline.
REGRESSED = SuiteResult(failed=(VISIBLE_TEST,), collected_files=COLLECTED)

HIDDEN_TEST = "tests/test_hidden.py::test_empty_config"
BENCH_COLLECTED = ("tests/test_app.py", "tests/test_hidden.py")
BENCH_BASELINE = SuiteResult(passed=(VISIBLE_TEST,), failed=(HIDDEN_TEST,), collected_files=BENCH_COLLECTED)
BENCH_AFTER = SuiteResult(passed=(VISIBLE_TEST, HIDDEN_TEST), collected_files=BENCH_COLLECTED)


@dataclass
class CheckoutAtExit:
    """What the checkout held when the task let go of it, read before the workspace deletes it."""

    path: Path | None = None
    head: str | None = None
    merge_in_progress: bool | None = None
    status: str | None = None
    base_sha: str | None = None
    git_config: bytes = b""


def recording_factory(url: str, seen: CheckoutAtExit):
    @asynccontextmanager
    async def factory(owner, name, target_branch, token_provider=None):
        async with task_workspace(owner, name, target_branch, token_provider=token_provider, clone_url=url) as ws:
            seen.path = ws.path
            try:
                yield ws
            finally:
                seen.head = git(ws.path, "rev-parse", "HEAD")
                seen.merge_in_progress = (ws.path / ".git" / "MERGE_HEAD").exists()
                seen.status = git(ws.path, "status", "--porcelain", "--untracked-files=no")
                seen.base_sha = ws.base_sha
                seen.git_config = (ws.path / ".git" / "config").read_bytes()

    return factory


@dataclass
class ScriptedResolver:
    """A `Resolver` whose behaviour the test writes; records what the pipeline handed it."""

    script: Callable[[ResolverDeps], Awaitable[ResolverResult]]
    calls: list[ResolverDeps] = field(default_factory=list)

    async def __call__(self, deps: ResolverDeps) -> ResolverResult:
        self.calls.append(deps)
        return await self.script(deps)


def stops_with(stop: ResolverStop) -> ScriptedResolver:
    async def script(deps):
        return ResolverResult(stop, None, rounds=1, steps=2)

    return ScriptedResolver(script)


def raises(exc: BaseException) -> ScriptedResolver:
    async def script(deps):
        raise exc

    return ScriptedResolver(script)


def moving_target(
    tmp_path: Path,
    origin_url: str,
    upstream: dict[str, str | None] | None,
    *,
    symlinks: dict[str, str] | None = None,
    rewrite: bool = False,
    ours: str = OURS,
    stop: StopReason = StopReason.SUBMITTED,
) -> tuple[ScriptedAgent, dict]:
    """An agent that edits and scores one attempt; then the target moves. Returns it and what it saw."""
    seen: dict = {}

    async def script(deps: AgentDeps):
        edit(deps, "src/app.py", ours)
        record = await deps.verify_attempt(1)
        seen["checkout"] = deps.checkout
        if rewrite:
            seen["tip"] = rewrite_upstream(origin_url, tmp_path / "upstream")
        elif upstream is not None or symlinks:
            seen["tip"] = push_upstream(origin_url, tmp_path / "upstream", upstream or {}, symlinks=symlinks)
        return agent_result(stop, record)

    return ScriptedAgent(script), seen


async def run(
    factory, origin_url, task, agent, backend, *, seen: CheckoutAtExit | None = None, **kwargs
) -> tuple[RunResult, FakeGithubClient]:
    github = FakeGithubClient()
    kwargs.setdefault("llm", NeverCalledLLM())
    result = await run_task(
        task.id,
        factory,
        github,
        backend,
        agent=agent,
        workspace_factory=recording_factory(origin_url, seen if seen is not None else CheckoutAtExit()),
        embedder_warmup=lambda: None,
        **kwargs,
    )
    return result, github


async def stages(factory, task_id) -> dict[str, str]:
    async with factory() as session:
        rows = await session.execute(select(TaskStage).where(TaskStage.task_id == task_id))
        return {row.stage: row.status for row in rows.scalars()}


def remote_branches(origin: Path) -> list[str]:
    return git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


def pushed(origin: Path, branch: str, path: str) -> str:
    return git(origin, "show", f"{branch}:{path}")


class TestTargetDidNotMove:
    async def test_the_stage_runs_and_changes_nothing(self, db_session, db_session_factory, origin_url, tmp_path):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, None)
        resolver = stops_with(ResolverStop.RESOLVED)
        backend = FakeBackend(results=[BASELINE, AFTER])  # a third suite run would fail the test

        result, github = await run(db_session_factory, origin_url, task, agent, backend, resolver=resolver)

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PR_OPENED
        assert (await stages(db_session_factory, task.id))["merge_target"] == "ok"
        assert [r.attempt for r in runs] == [0, 1]
        assert resolver.calls == []
        assert row.conflict_reason is None and result.conflict_reason is None
        assert "target branch" not in github.pull_requests[0]["body"]
        assert result.pr_gate_reason == "the agent submitted and no evidence of harm was found"


class TestCleanMerge:
    async def test_the_merged_tree_is_scored_and_the_pr_is_the_agents_change_on_the_new_tip(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, saw = moving_target(tmp_path, origin_url, {"README.md": README_UPSTREAM})
        seen = CheckoutAtExit()

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER, AFTER]), seen=seen
        )

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PR_OPENED
        assert [r.attempt for r in runs] == [0, 1, 2], "the pre-merge attempt stays; the merged tree is attempt 2"
        assert runs[2].commit_sha != runs[1].commit_sha
        assert seen.base_sha == saw["tip"], "the base advanced to the target's new tip"
        branch = github.pull_requests[0]["head"]
        assert git(origin, "rev-parse", f"{branch}^") == saw["tip"], "squashed onto the new tip"
        assert pushed(origin, branch, "README.md") == README_UPSTREAM.rstrip("\n")
        assert "Upstream documentation" not in row.patch_diff, "the PR shows the agent's change only"
        assert row.changed_files == ["src/app.py"]
        assert "| target branch | moved while the task ran; merged in cleanly |" in github.pull_requests[0]["body"]
        assert result.attempts == 1 and row.retry_count == 0, "the merge attempt is not an agent retry"

    async def test_the_gate_reads_the_merged_attempt_not_the_pre_merge_one(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, {"README.md": README_UPSTREAM})

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER, REGRESSED])
        )

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.COMPLETED
        assert github.pull_requests == []
        assert "flagged the change" in result.pr_gate_reason and VISIBLE_TEST in result.pr_gate_reason
        assert row.patch_sha == runs[2].commit_sha
        assert row.conflict_reason is None

    async def test_a_target_that_moved_without_changing_a_file_keeps_the_old_score(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, saw = moving_target(tmp_path, origin_url, {})  # an empty commit
        seen = CheckoutAtExit()

        result, github = await run(  # a third suite run would fail the test
            db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]), seen=seen
        )

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PR_OPENED
        assert [r.attempt for r in runs] == [0, 1], "the merged tree is the scored one; nothing to re-run"
        assert seen.base_sha == saw["tip"]
        assert git(origin, "rev-parse", f"{github.pull_requests[0]['head']}^") == saw["tip"]

    async def test_a_target_that_already_has_the_change_gets_no_pr(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, {"src/app.py": OURS})
        seen = CheckoutAtExit()

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]), seen=seen
        )

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.COMPLETED
        assert github.pull_requests == [] and result.pr_gate_reason == ALREADY_CONTAINED_REASON
        assert [r.attempt for r in runs] == [0, 1]
        assert seen.head == row.patch_sha == runs[1].commit_sha, "the merge was undone"
        assert "edited by the agent" in row.patch_diff


class TestResolvedConflict:
    async def test_a_both_modified_conflict_is_resolved_committed_and_rescored(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, saw = moving_target(tmp_path, origin_url, {"src/app.py": THEIRS})
        checks: dict[str, list[str]] = {}

        async def resolve(deps: ResolverDeps) -> ResolverResult:
            checks["before"] = await deps.check(deps.conflicts)
            (deps.checkout / "src" / "app.py").write_text(RESOLVED)
            checks["after"] = await deps.check(deps.conflicts)
            return ResolverResult(ResolverStop.RESOLVED, "Kept both lines.", rounds=1, steps=1)

        resolver = ScriptedResolver(resolve)
        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER, AFTER]),
            resolver=resolver,
        )

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PR_OPENED
        (deps,) = resolver.calls
        assert deps.conflicts == ("src/app.py",), "exactly the conflicted set, nothing else"
        assert deps.theirs_sha == saw["tip"] and deps.base_sha != saw["tip"]
        assert deps.is_protected("tests/test_app.py"), "the baseline-aware guard the main agent gets"
        assert not deps.is_protected("src/app.py")
        assert any("still holds a conflict marker line" in p for p in checks["before"])
        assert checks["after"] == []
        assert [r.attempt for r in runs] == [0, 1, 2]
        branch = github.pull_requests[0]["head"]
        assert pushed(origin, branch, "src/app.py") == RESOLVED.rstrip("\n")
        assert git(origin, "rev-parse", f"{branch}^") == saw["tip"]
        assert "+# edited by the agent" in row.patch_diff and "+# upstream change" not in row.patch_diff
        assert "1 conflicted file(s) reconciled" in github.pull_requests[0]["body"]
        assert "Kept both lines" not in github.pull_requests[0]["body"], "the resolver's own account is not shown"

    async def test_the_real_resolver_cannot_write_git_or_a_file_outside_the_conflict(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, saw = moving_target(tmp_path, origin_url, {"src/app.py": THEIRS})
        at_git = call("edit_file", path=".git/config", old_string="[core]", new_string="[core]\n\thooksPath = /tmp/x")
        at_readme = call("edit_file", path="README.md", old_string="# sample", new_string="# hijacked")
        resolution: dict = {}

        def resolve_file(_request):
            current = (saw["checkout"] / "src" / "app.py").read_text()
            resolution["call"] = call("edit_file", path="src/app.py", old_string=current, new_string=RESOLVED)
            return reply(resolution["call"])

        provider = ScriptedProvider(
            [
                reply(at_git),
                reply(at_readme),
                resolve_file,
                reply(call("submit_resolution", summary="Kept the agent's line and the upstream line.")),
            ]
        )
        seen = CheckoutAtExit()
        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER, AFTER]),
            seen=seen, llm=make_client(db_session_factory, provider), resolver=resolve_conflicts,
        )

        assert result.status is TaskStatus.PR_OPENED, result
        assert provider.unused == 0, "every scripted reply was asked for, and no more"
        assert ".git" in provider.result_of(at_git) and "edited" not in provider.result_of(at_git)
        assert "only change the conflicted files" in provider.result_of(at_readme)
        assert b"hooksPath" not in seen.git_config
        branch = github.pull_requests[0]["head"]
        assert pushed(origin, branch, "README.md") == "# sample"
        assert pushed(origin, branch, "src/app.py") == RESOLVED.rstrip("\n")
        calls = await llm_rows(db_session_factory, task.id)
        assert [c.stage for c in calls] == ["resolve"] * 4
        row, _ = await reload(db_session_factory, task.id)
        assert row.cost_usd == Decimal("0.0024"), "the resolver's spend is the task's"


class TestUnresolvable:
    async def assert_conflicting(self, factory, task, result, github, origin, seen, *, reason: str):
        row, runs = await reload(factory, task.id)
        assert result.status is row.status is TaskStatus.CONFLICTING
        assert reason in row.conflict_reason and result.conflict_reason == row.conflict_reason
        assert row.error_message is None and result.error_message is None
        assert github.pull_requests == [] and row.pr_number is None
        assert remote_branches(origin) == ["main"], "nothing was pushed"
        assert [r.attempt for r in runs] == [0, 1]
        assert row.outcome is TaskOutcome.PASSED, "the outcome the pre-merge attempt scored"
        assert seen.head == row.patch_sha == runs[1].commit_sha, "the checkout is back at the scored commit"
        assert seen.merge_in_progress is False and seen.status == ""
        assert "edited by the agent" in row.patch_diff
        assert row.agent_stop_reason == "submitted"
        return row

    async def test_a_resolver_that_gives_up_leaves_the_task_conflicting(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, {"src/app.py": THEIRS})
        seen = CheckoutAtExit()

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[RED_BASELINE, GREEN]),
            seen=seen, resolver=stops_with(ResolverStop.GAVE_UP),
        )

        row = await self.assert_conflicting(
            db_session_factory, task, result, github, origin, seen, reason="the conflict resolver stopped (gave_up)"
        )
        assert "src/app.py" in row.conflict_reason
        assert (await stages(db_session_factory, task.id))["merge_target"] == "ok"

    async def test_a_delete_modify_conflict_never_reaches_the_resolver(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, {"src/app.py": None})
        resolver = stops_with(ResolverStop.RESOLVED)
        seen = CheckoutAtExit()

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[RED_BASELINE, GREEN]),
            seen=seen, resolver=resolver,
        )

        await self.assert_conflicting(db_session_factory, task, result, github, origin, seen, reason="delete_modify")
        assert resolver.calls == []

    async def test_a_symlink_conflict_never_reaches_the_resolver(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, {}, symlinks={"src/app.py": "../README.md"})
        resolver = stops_with(ResolverStop.RESOLVED)
        seen = CheckoutAtExit()

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[RED_BASELINE, GREEN]),
            seen=seen, resolver=resolver,
        )

        await self.assert_conflicting(db_session_factory, task, result, github, origin, seen, reason="symlink")
        assert resolver.calls == []

    async def test_a_rewritten_target_is_not_merged(self, db_session, db_session_factory, origin_url, origin, tmp_path):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, None, rewrite=True)
        resolver = stops_with(ResolverStop.RESOLVED)
        seen = CheckoutAtExit()

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[RED_BASELINE, GREEN]),
            seen=seen, resolver=resolver,
        )

        await self.assert_conflicting(db_session_factory, task, result, github, origin, seen, reason="rewritten")
        assert resolver.calls == []

    async def test_no_model_client_means_no_resolution(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, {"src/app.py": THEIRS})
        seen = CheckoutAtExit()

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[RED_BASELINE, GREEN]),
            seen=seen, llm=None,
        )

        await self.assert_conflicting(db_session_factory, task, result, github, origin, seen, reason="no model client")

    async def test_a_budget_stop_escaping_the_resolver_is_still_the_resolvers(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, {"src/app.py": THEIRS})
        seen = CheckoutAtExit()
        exceeded = BudgetExceeded(BudgetLimit.USD, spent_usd=Decimal("0.5"), calls=3, elapsed_seconds=1.0)

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[RED_BASELINE, GREEN]),
            seen=seen, resolver=raises(exceeded),
        )

        await self.assert_conflicting(
            db_session_factory, task, result, github, origin, seen, reason="the conflict resolver stopped (budget)"
        )

    async def test_a_resolution_the_host_refuses_is_not_committed(
        self, db_session, db_session_factory, origin_url, origin, tmp_path
    ):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, {"src/app.py": THEIRS})
        seen = CheckoutAtExit()

        async def claims_resolved(deps):
            return ResolverResult(ResolverStop.RESOLVED, "done", rounds=1, steps=1)  # markers still there

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[RED_BASELINE, GREEN]),
            seen=seen, resolver=ScriptedResolver(claims_resolved),
        )

        await self.assert_conflicting(
            db_session_factory, task, result, github, origin, seen, reason="the host refused the resolver's resolution"
        )


class TestHarnessFailure:
    async def test_a_provider_that_refuses_us_fails_the_task(
        self, db_session, db_session_factory, origin_url, tmp_path
    ):
        task = await seed_task(db_session)
        agent, _ = moving_target(tmp_path, origin_url, {"src/app.py": THEIRS})
        refused = LLMCallError("provider auth failure: key rejected", stage="resolve", model="m", retries=0, kind=KIND_AUTH)

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]),
            resolver=raises(refused),
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.FAILED
        assert row.error_message.startswith("merge_target: ") and "provider auth failure" in row.error_message
        assert row.conflict_reason is None and github.pull_requests == []
        assert (await stages(db_session_factory, task.id))["merge_target"] == "failed"


class TestWhenTheStageDoesNotRun:
    async def test_a_task_that_would_open_no_pr_never_fetches(
        self, db_session, db_session_factory, origin_url, tmp_path
    ):
        task = await seed_task(db_session)
        agent, saw = moving_target(tmp_path, origin_url, {"src/app.py": THEIRS}, stop=StopReason.STEP_CAP)
        resolver = stops_with(ResolverStop.RESOLVED)

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]), resolver=resolver
        )

        assert git(Path(origin_url.removeprefix("file://")), "rev-parse", "main") == saw["tip"], "the target did move"

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.COMPLETED, "not conflicting: no PR was going to open"
        assert row.conflict_reason is None and resolver.calls == [] and github.pull_requests == []
        assert "merge_target" not in await stages(db_session_factory, task.id)

    async def test_a_benchmark_task_keeps_its_score_on_the_curated_tree(
        self, db_session, db_session_factory, origin_url, origin, source_repo, tmp_path
    ):
        directory = tmp_path / "instances"
        directory.mkdir()
        instance = make_instance(base_commit=git(source_repo, "rev-parse", "HEAD"))
        dump_instance(instance, directory / f"{instance.instance_id}.json")
        task = await seed_task(db_session, instance_id=instance.instance_id, eval_run_id="run-1", run_index=0)
        agent, saw = moving_target(tmp_path, origin_url, {"src/app.py": THEIRS})
        resolver = stops_with(ResolverStop.RESOLVED)

        result, github = await run(
            db_session_factory, origin_url, task, agent, FakeBackend(results=[BENCH_BASELINE, BENCH_AFTER]),
            resolver=resolver, instances_dir=directory,
        )

        assert git(origin, "rev-parse", "main") == saw["tip"], "the target did move"

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PR_OPENED
        assert row.outcome is TaskOutcome.PASSED and len(github.pull_requests) == 1
        assert [r.attempt for r in runs] == [0, 1] and resolver.calls == []
        assert "merge_target" not in await stages(db_session_factory, task.id)
