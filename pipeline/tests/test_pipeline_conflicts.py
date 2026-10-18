"""The parts of `conflicts.py` that need no database: the resolver's budget, the host's check, the note.

The check runs against a real mid-merge checkout (a bare local remote that moved after the clone), not
a hand-built tree with markers in it, because what it reads -- unmerged index entries, worktree
changes -- only exists in a real merge.
"""

import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from pipeline_support import APP_SOURCE, push_upstream, write
from repolace_gateway.budget import BudgetExceeded, TaskBudget
from repolace_pipeline import conflicts
from repolace_pipeline.conflicts import (
    RESOLVER_MAX_CALLS,
    RESOLVER_MAX_USD,
    RESOLVER_MAX_WALL_SECONDS,
    MergeOutcome,
    TargetMerge,
    host_check,
    resolver_budget,
)
from repolace_shared.git import task_workspace
from repolace_shared.git import workspace as workspace_module

pytestmark = pytest.mark.anyio

TASK_ID = uuid.UUID("2f8a1c4e-0000-4000-8000-0000000000c1")
OURS = APP_SOURCE + "\n# ours\n"
THEIRS = APP_SOURCE + "\n# theirs\n"
RESOLVED = APP_SOURCE + "\n# ours\n# theirs\n"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TestResolverBudget:
    def test_it_is_half_of_what_the_agent_has_left(self):
        clock = Clock()
        agent = TaskBudget(max_usd=Decimal("0.60"), max_calls=30, max_wall_seconds=1000.0, clock=clock)
        agent.charge(Decimal("0.20"))
        agent.charge(None)
        clock.now = 400.0

        budget = resolver_budget(agent)

        assert budget.max_usd == Decimal("0.20")
        assert budget.max_calls == 14
        assert budget.max_wall_seconds == 300.0
        assert (budget.spent_usd, budget.calls) == (Decimal(0), 0), "a budget of its own, not the agent's"

    def test_it_never_exceeds_the_caps(self):
        budget = resolver_budget(TaskBudget())

        assert budget.max_usd == RESOLVER_MAX_USD == Decimal("0.50")
        assert budget.max_calls == RESOLVER_MAX_CALLS == 40
        assert budget.max_wall_seconds == RESOLVER_MAX_WALL_SECONDS == 600.0

    def test_an_agent_that_spent_everything_leaves_the_resolver_nothing(self):
        agent = TaskBudget(max_usd=Decimal("1.00"))
        agent.charge(Decimal("1.25"))

        with pytest.raises(BudgetExceeded):
            resolver_budget(agent).raise_if_reached()

    def test_it_runs_on_the_agents_clock(self):
        clock = Clock()
        budget = resolver_budget(TaskBudget(clock=clock))
        clock.now = 601.0

        with pytest.raises(BudgetExceeded):
            budget.raise_if_reached()


@asynccontextmanager
async def mid_merge(origin_url, tmp_path):
    """A checkout whose agent branch and the moved target both appended to `src/app.py`, mid-merge."""
    async with task_workspace("acme", "sample", "main", clone_url=origin_url) as workspace:
        await workspace.start_agent_branch(7, TASK_ID)
        write(workspace.path / "src" / "app.py", OURS)
        await workspace.record_attempt("attempt 1")
        push_upstream(origin_url, tmp_path / "upstream", {"src/app.py": THEIRS})
        _, tip = await workspace.target_moved()
        assert not await workspace.repo.merge_no_commit(tip)
        yield workspace


class TestHostCheck:
    async def test_a_marker_is_reported_with_its_line_and_a_resolved_file_is_not(self, origin_url, tmp_path):
        async with mid_merge(origin_url, tmp_path) as workspace:
            check = host_check(workspace, ["src/app.py"])

            problems = await check(["src/app.py"])
            assert len(problems) == 1 and problems[0].startswith("src/app.py:")
            assert "still holds a conflict marker line" in problems[0]

            write(workspace.path / "src" / "app.py", RESOLVED)
            assert await check(["src/app.py"]) == []

    async def test_an_edit_outside_the_conflicted_set_is_a_problem(self, origin_url, tmp_path):
        async with mid_merge(origin_url, tmp_path) as workspace:
            write(workspace.path / "src" / "app.py", RESOLVED)
            write(workspace.path / "README.md", "# changed behind the host's back\n")

            problems = await host_check(workspace, ["src/app.py"])(["src/app.py"])

            assert problems == ["1 file(s) outside the conflicted set were changed: README.md"]

    async def test_a_symlink_in_place_of_the_conflicted_file_is_refused(self, origin_url, tmp_path):
        async with mid_merge(origin_url, tmp_path) as workspace:
            target = workspace.path / "src" / "app.py"
            target.unlink()
            target.symlink_to(workspace.path / "README.md")

            problems = await host_check(workspace, ["src/app.py"])(["src/app.py"])

            assert any(p.startswith("src/app.py:") and "marker" not in p for p in problems)

    async def test_a_path_outside_the_checkout_is_refused_not_read(self, origin_url, tmp_path):
        async with mid_merge(origin_url, tmp_path) as workspace:
            problems = await host_check(workspace, ["../outside.py"])(["../outside.py"])

            assert "../outside.py: not a file inside the checkout" in problems

    async def test_an_unmerged_file_left_out_of_the_set_is_a_problem(self, origin_url, tmp_path):
        async with mid_merge(origin_url, tmp_path) as workspace:
            write(workspace.path / "src" / "app.py", RESOLVED)
            write(workspace.path / "src" / "extra.py", "x = 1\n")

            problems = await host_check(workspace, ["src/extra.py"])(["src/extra.py"])

            assert "1 unmerged file(s) outside the conflicted set: src/app.py" in problems


def test_the_marker_rule_has_one_definition():
    """The resolver's check and `commit_resolution` must agree on what a marker is: they share one function."""
    assert conflicts.file_has_marker is workspace_module.file_has_marker
    assert not hasattr(conflicts, "_MARKER_LINE")


class Recorder:
    """A resolver that must not be reached; remembers if it was."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, deps):
        self.called = True
        raise AssertionError("the resolver was called")


async def run_resolve(workspace, resolver):
    _, tip = await workspace.target_moved()
    return await conflicts._resolve(
        task_id=TASK_ID,
        workspace=workspace,
        issue=None,
        baseline=None,
        hidden_paths=(),
        llm=object(),
        resolver=resolver,
        agent_budget=TaskBudget(),
        old_base=workspace.base_sha,
        tip=tip,
        message="merge",
    )


class TestUnverifiableConflictsAreRefused:
    async def test_attributes_that_change_the_markers_end_conflicting(self, origin_url, tmp_path):
        async with mid_merge(origin_url, tmp_path) as workspace:
            write(workspace.path / ".gitattributes", "src/app.py conflict-marker-size=9\n")
            resolver = Recorder()

            result = await run_resolve(workspace, resolver)

            assert isinstance(result, str) and "gitattributes" in result
            assert not resolver.called

    async def test_a_conflicted_file_with_no_markers_ends_conflicting(self, origin_url, tmp_path):
        """A binary or `-merge` conflict leaves our side in the file; submitting it unedited would drop theirs."""
        async with mid_merge(origin_url, tmp_path) as workspace:
            write(workspace.path / "src" / "app.py", OURS)
            resolver = Recorder()

            result = await run_resolve(workspace, resolver)

            assert isinstance(result, str) and "hold no conflict markers" in result
            assert not resolver.called

    async def test_too_many_unmerged_files_end_conflicting(self, origin_url, tmp_path, monkeypatch):
        from repolace_shared.git import repo as repo_module

        async with mid_merge(origin_url, tmp_path) as workspace:
            monkeypatch.setattr(repo_module, "MAX_UNMERGED_PATHS", 0)

            result = await run_resolve(workspace, Recorder())

            assert isinstance(result, str) and "more unmerged files" in result

    async def test_a_conflict_with_no_unmerged_entries_ends_conflicting(self, origin_url, tmp_path, monkeypatch):
        async with mid_merge(origin_url, tmp_path) as workspace:

            async def none(self):
                return []

            monkeypatch.setattr(type(workspace.repo), "unmerged_files", none)

            result = await run_resolve(workspace, Recorder())

            assert isinstance(result, str) and "no unmerged files" in result


def test_a_reason_cannot_carry_control_characters():
    assert conflicts._clean("a\x1b[31mb\nc\u202ed", 100) == "a?[31mb?c?d"
    assert len(conflicts._clean("x" * 500, 300)) == 300


class TestNote:
    def test_only_a_merge_has_one(self):
        for outcome in (MergeOutcome.NOT_MOVED, MergeOutcome.ALREADY_CONTAINED, MergeOutcome.CONFLICTING):
            assert TargetMerge(outcome, "a" * 40, reason="x").note is None

    def test_it_says_whether_the_resolver_was_involved(self):
        assert TargetMerge(MergeOutcome.MERGED, "a" * 40).note == "moved while the task ran; merged in cleanly"
        assert "2 conflicted file(s) reconciled by repolace's conflict resolver" in TargetMerge(
            MergeOutcome.MERGED, "a" * 40, resolved_files=2
        ).note
