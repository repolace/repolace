"""What the `Task` model declares, checked without a database.

`alembic check` compares columns, types and indexes, but not CHECK constraints
and, in practice, not a partial index's `WHERE` predicate -- so a model that
drifts from its migration on either is invisible to it. The database-backed
tests read the *migrated* catalog and nothing reads the model side. These close
that gap, and they are in their own module because the one they were written
beside carries a module-level `db` mark, which skipped even the tests that never
needed a server.
"""

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import CheckConstraint, Index

from repolace_shared.db.models import AGENT_STOP_REASONS, Task

MIGRATION_0013 = (
    Path(__file__).resolve().parents[2]
    / "api"
    / "alembic"
    / "versions"
    / "0013_add_agent_outputs_and_index_strategy.py"
)


def _declared_checks() -> dict[str, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in Task.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _declared_indexes() -> dict[str, Index]:
    return {index.name: index for index in Task.__table__.indexes}


class _RecordingOp:
    """Stands in for `alembic.op` so a migration's `upgrade()` runs with no database."""

    def __init__(self) -> None:
        self.checks: dict[str, str] = {}
        self.indexes: dict[str, SimpleNamespace] = {}

    def add_column(self, *args, **kwargs) -> None:
        pass

    def create_check_constraint(self, name, table, condition, **kwargs) -> None:
        self.checks[name] = condition

    def create_index(self, name, table, columns, **kwargs) -> None:
        self.indexes[name] = SimpleNamespace(columns=list(columns), **kwargs)


@pytest.fixture(scope="module")
def migration_0013():
    """What 0013's `upgrade()` asks Alembic to create, recorded rather than executed."""
    spec = importlib.util.spec_from_file_location("migration_0013_under_test", MIGRATION_0013)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = _RecordingOp()
    module.op = recorder
    module.upgrade()
    return recorder


class TestStopReasonConstant:
    def test_the_stop_reasons_are_the_eight_the_contract_names(self):
        """Pinned as literals so adding or renaming one is a deliberate edit here,
        in `repolace_agents.contracts.StopReason` and in a migration -- not a
        side effect of editing one of the three."""
        assert AGENT_STOP_REASONS == (
            "submitted",
            "step_cap",
            "budget_usd",
            "budget_calls",
            "budget_wall",
            "llm_error",
            "no_change",
            "max_attempts",
        )

    def test_they_are_unique(self):
        assert len(set(AGENT_STOP_REASONS)) == len(AGENT_STOP_REASONS)

    def test_the_model_check_lists_exactly_the_constant(self):
        """Every reason, and nothing else, appears in the model's CHECK text."""
        text = _declared_checks()["ck_tasks_agent_stop_reason"]

        assert set(re.findall(r"'([a-z_]+)'", text)) == set(AGENT_STOP_REASONS)

    def test_the_migrations_own_copy_agrees_at_this_revision(self, migration_0013):
        """0013 is the revision that created the list, so its snapshot and the live
        constant are equal *here*. A later migration that extends the list will
        change the constant and leave this one alone, and this test is then
        deleted with it -- which is the point of a snapshot."""
        text = migration_0013.checks["ck_tasks_agent_stop_reason"]

        assert tuple(re.findall(r"'([a-z_]+)'", text)) == AGENT_STOP_REASONS


class TestEvalColumnsCheck:
    NAME = "ck_tasks_eval_columns_together"

    def test_the_model_declares_it(self):
        assert self.NAME in _declared_checks()

    def test_it_requires_all_three_columns_null_or_all_set(self):
        """The text, not just the name: a CHECK with the right name and the wrong
        predicate is the failure `alembic check` cannot see."""
        normalised = " ".join(_declared_checks()[self.NAME].split())

        assert normalised == (
            "(eval_run_id IS NULL) = (instance_id IS NULL) AND (eval_run_id IS NULL) = (run_index IS NULL)"
        )

    def test_the_migration_creates_the_same_predicate(self, migration_0013):
        """Model and migration are two copies; this is the only thing holding them equal."""
        assert " ".join(migration_0013.checks[self.NAME].split()) == " ".join(
            _declared_checks()[self.NAME].split()
        )


class TestEvalInstanceRunIndexDeclaration:
    NAME = "uq_tasks_eval_instance_run"

    def test_the_model_declares_a_unique_index_over_the_three_columns(self):
        index = _declared_indexes()[self.NAME]

        assert index.unique is True
        assert [column.name for column in index.columns] == ["eval_run_id", "instance_id", "run_index"]

    def test_the_model_index_is_partial_on_eval_run_id(self):
        """The `WHERE` is what leaves product tasks free to repeat. `alembic check`
        does not compare it, so a model that lost it would pass and the next
        autogenerate would silently rewrite the index."""
        index = _declared_indexes()[self.NAME]

        assert str(index.dialect_options["postgresql"]["where"]) == "eval_run_id IS NOT NULL"

    def test_the_migration_creates_the_same_index(self, migration_0013):
        created = migration_0013.indexes[self.NAME]
        declared = _declared_indexes()[self.NAME]

        assert created.columns == [column.name for column in declared.columns]
        assert created.unique is True
        assert str(created.postgresql_where) == str(declared.dialect_options["postgresql"]["where"])
