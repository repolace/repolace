"""The report labels a task by its editing (`agent`) stage and prints a mixed run's stage map."""

from dataclasses import replace
from datetime import timedelta

import pytest
from eval_stage_models_support import EDITOR, STRONG
from eval_support import T0, add_call, add_eval_task, rows_for, run_manifest, seed_bench_repo
from harness.report import aggregate, load_rows, to_markdown
from repolace_shared.db.models import TaskOutcome


class TestTheStageMapIsShown:
    def mixed(self):
        return run_manifest(2, runs=1, model=EDITOR, stage_models={"agent": EDITOR, "plan": STRONG, "review": STRONG})

    def rows(self):
        return rows_for([TaskOutcome.PASSED, TaskOutcome.FAILED], model=EDITOR)

    def test_the_map_is_in_the_headline_and_the_read_this_first_list(self):
        text = to_markdown(aggregate(self.rows(), manifests={"run-a": self.mixed()}))
        line = f"Stage models: agent={EDITOR}, plan={STRONG}, review={STRONG}"

        assert text.count(line) == 2
        first = text.split("## Read this first")[1].split("## Headline")[0]
        assert line in first and "do not pool it with a single-model run" in first

    def test_a_single_model_run_prints_no_map(self):
        text = to_markdown(aggregate(self.rows(), manifests={"run-a": run_manifest(2, runs=1, model=EDITOR)}))

        assert "Stage models:" not in text

    def test_a_run_without_a_manifest_prints_no_map(self):
        assert "Stage models:" not in to_markdown(aggregate(self.rows()))

    def test_a_withheld_headline_still_names_the_map(self):
        rows = rows_for([TaskOutcome.PASSED, TaskOutcome.FAILED], model=EDITOR)
        rows[1] = replace(rows[1], model="other-editor")

        text = to_markdown(aggregate(rows, manifests={"run-a": self.mixed()}))

        assert "mixed models within the run" in text and "Stage models: agent=" in text


@pytest.mark.anyio
@pytest.mark.db
class TestTheLabel:
    async def test_a_task_is_labelled_by_its_agent_call_although_the_planner_called_first(self, db_session):
        repo = await seed_bench_repo(db_session)
        task = await add_eval_task(db_session, repo, outcome=TaskOutcome.PASSED)
        add_call(db_session, task, model=STRONG, stage="plan", created_at=T0)
        add_call(db_session, task, model=EDITOR, stage="agent", created_at=T0 + timedelta(seconds=5))
        await db_session.commit()

        (row,) = await load_rows(db_session, ["run-a"])

        assert row.model == EDITOR

    async def test_a_task_with_no_agent_call_falls_back_to_its_first_call(self, db_session):
        repo = await seed_bench_repo(db_session)
        task = await add_eval_task(db_session, repo, outcome=TaskOutcome.FAILED)
        add_call(db_session, task, model=STRONG, stage="plan", created_at=T0)
        add_call(db_session, task, model="later-model", stage="review", created_at=T0 + timedelta(seconds=5))
        await db_session.commit()

        (row,) = await load_rows(db_session, ["run-a"])

        assert row.model == STRONG
