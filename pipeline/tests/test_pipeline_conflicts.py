"""The pieces of `conflicts` that need no database: the resolver's budget and the host's check.

The whole flow, through `run_task`, is in `test_pipeline_conflicts_db.py`.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from repolace_gateway.budget import TaskBudget
from repolace_pipeline.conflicts import (
    RESOLVER_MAX_USD,
    RESOLVER_MAX_WALL_SECONDS,
    file_problems,
    resolver_budget,
)
from repolace_shared.git.workspace import _refuse_markers


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TestTheResolversBudget:
    def test_it_is_half_of_what_the_agent_has_left(self):
        clock = FakeClock()
        agent = TaskBudget(max_usd=Decimal("0.60"), max_calls=150, max_wall_seconds=1000.0, clock=clock)
        agent.charge(Decimal("0.20"))
        clock.now = 400.0

        budget = resolver_budget(agent)

        assert budget.max_usd == Decimal("0.20")
        assert budget.max_wall_seconds == 300.0
        assert budget.spent_usd == 0 and budget.calls == 0, "its own budget, starting empty"

    def test_it_is_never_more_than_the_caps(self):
        budget = resolver_budget(TaskBudget(max_usd=Decimal("2.00"), max_wall_seconds=3600.0))

        assert budget.max_usd == RESOLVER_MAX_USD == Decimal("0.50")
        assert budget.max_wall_seconds == RESOLVER_MAX_WALL_SECONDS == 600.0
        assert budget.max_calls == 40, "half of 150 left, capped at twice the resolver's 20 steps"

    def test_an_agent_that_spent_everything_leaves_nothing(self):
        clock = FakeClock()
        agent = TaskBudget(max_usd=Decimal("1.00"), max_calls=2, max_wall_seconds=10.0, clock=clock)
        agent.charge(Decimal("1.50"))
        agent.charge(None)
        clock.now = 20.0

        budget = resolver_budget(agent)

        assert (budget.max_usd, budget.max_calls, budget.max_wall_seconds) == (0, 0, 0.0)

    def test_it_shares_the_agents_clock(self):
        clock = FakeClock()
        budget = resolver_budget(TaskBudget(clock=clock))
        clock.now = 30.0

        assert budget.elapsed_seconds == 30.0


CASES = {
    "clean.py": "def f():\n    return 1\n",
    "ours.py": "<<<<<<< ours\nx = 1\n=======\nx = 2\n>>>>>>> theirs\n",
    "bare.py": "a\n<<<<<<<\nb\n",
    "base.py": "a\n||||||| base\nb\n",
    "crlf.py": "a\r\n=======\r\nb\r\n",
    "heading.md": "Title\n=======\n",
    "eight.py": "<<<<<<<< not a marker\n========\n",
    "indented.py": "x = '''\n  =======\n'''\n",
    "inline.py": "s = '<<<<<<< ours'\n",
}


class TestTheHostsCheck:
    @pytest.mark.parametrize("name", sorted(CASES))
    def test_it_agrees_with_commit_resolution_about_every_file(self, tmp_path: Path, name: str):
        """A check that passes a file `commit_resolution` then refuses would end a resolved run as refused."""
        (tmp_path / name).write_bytes(CASES[name].encode())

        problems = file_problems(tmp_path, name)
        try:
            _refuse_markers(tmp_path / name, name)
            refused = False
        except RuntimeError:
            refused = True

        assert bool(problems) == refused

    def test_it_names_the_marker_lines(self, tmp_path: Path):
        (tmp_path / "ours.py").write_text(CASES["ours.py"])

        (problem,) = file_problems(tmp_path, "ours.py")

        assert "ours.py" in problem and "line 1, 3, 5" in problem

    def test_a_deleted_file_or_a_symlink_is_a_problem(self, tmp_path: Path):
        (tmp_path / "target.py").write_text("x = 1\n")
        (tmp_path / "link.py").symlink_to("target.py")

        assert file_problems(tmp_path, "gone.py")
        assert file_problems(tmp_path, "link.py")
        assert file_problems(tmp_path, "../outside.py")
