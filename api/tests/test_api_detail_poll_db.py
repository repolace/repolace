"""The task detail poll must not read the large columns while a task is live.

The detail page polls `/ui/tasks/{id}/live` every 3 s for as long as a task runs, and
the JSON detail and the list-row fragment are polled the same way. `patch_diff`,
`score_reason` and `issue_body` are unbounded text, and none of them has a value
until the task ends (the pipeline writes the first two in the same UPDATE that ends it),
so a poll that fetches them moves megabytes for nothing, every tick, per open tab.

These tests read the statements the engine actually sent, not the ORM objects: a
column that was loaded and then ignored is exactly what they have to catch.
"""

import pytest
from api_support import capture_sql, seed_repo, seed_task
from repolace_shared.db.models import Task, TaskOutcome, TaskStatus
from sqlalchemy import update
from ui_support import ui_client

pytestmark = [pytest.mark.anyio, pytest.mark.db]

BIG = ("patch_diff", "score_reason", "issue_body")
DIFF = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n"


def selected(statements: list[tuple[str, tuple]], column: str) -> bool:
    return any(column in sql for sql, _ in statements)


async def fetch(db_engine, db_session_factory, path: str):
    async with ui_client(db_session_factory) as client:
        with capture_sql(db_engine) as seen:
            response = await client.get(path)
    return response, seen


def poll_paths(task_id) -> list[str]:
    return [f"/tasks/{task_id}", f"/ui/tasks/{task_id}", f"/ui/tasks/{task_id}/live", f"/ui/tasks/{task_id}/row"]


class TestLiveTasksDoNotLoadLargeColumns:
    @pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.RUNNING])
    async def test_no_poll_of_a_live_task_selects_them(self, db_session, db_session_factory, db_engine, status):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=status, issue_body="the issue body")

        for path in poll_paths(task.id):
            response, seen = await fetch(db_engine, db_session_factory, path)
            assert response.status_code == 200, path
            assert seen, path  # the listener is attached to the engine the request used
            for column in BIG:
                assert not selected(seen, column), (path, column)

    async def test_a_live_tasks_json_shows_them_as_null(self, db_session, db_session_factory, db_engine):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING, issue_body="the issue body")

        response, _ = await fetch(db_engine, db_session_factory, f"/tasks/{task.id}")

        body = response.json()
        assert body["patch_diff"] is None and body["score_reason"] is None
        assert "the issue body" not in response.text  # TaskDetail never carried it


class TestTerminalTasksStillRenderThem:
    async def seed_terminal(self, db_session, **extra):
        repo = await seed_repo(db_session)
        return await seed_task(
            db_session,
            repo,
            status=TaskStatus.PR_OPENED,
            outcome=TaskOutcome.PASSED,
            score_reason="Every curated test passes.",
            patch_diff=DIFF,
            pr_url="https://github.com/acme/sample/pull/5",
            pr_number=5,
            issue_body="the issue body",
            **extra,
        )

    async def test_the_json_and_the_page_carry_the_diff_and_the_sentence(
        self, db_session, db_session_factory, db_engine
    ):
        task = await self.seed_terminal(db_session)

        response, seen = await fetch(db_engine, db_session_factory, f"/tasks/{task.id}")
        page, _ = await fetch(db_engine, db_session_factory, f"/ui/tasks/{task.id}")

        body = response.json()
        assert body["patch_diff"] == DIFF and body["score_reason"] == "Every curated test passes."
        assert "Every curated test passes." in page.text and "1 file changed" in page.text
        # Positive control: the listener does see column names, so the negative cases above mean something.
        assert selected(seen, "patch_diff") and selected(seen, "score_reason")
        assert not selected(seen, "issue_body")

    async def test_the_same_task_polls_light_while_running_and_full_once_finished(
        self, db_session, db_session_factory, db_engine
    ):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING, issue_body="the issue body")

        running, seen_running = await fetch(db_engine, db_session_factory, f"/ui/tasks/{task.id}/live")
        assert running.status_code == 200
        assert not selected(seen_running, "patch_diff")

        async with db_session_factory() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    status=TaskStatus.COMPLETED,
                    outcome=TaskOutcome.FAILED,
                    patch_diff=DIFF,
                    score_reason="regression in a baseline test",
                )
            )
            await session.commit()

        done, seen_done = await fetch(db_engine, db_session_factory, f"/ui/tasks/{task.id}/live")
        assert done.status_code == 286  # htmx stops polling
        assert "regression in a baseline test" in done.text and "1 file changed" in done.text
        assert selected(seen_done, "patch_diff")

    async def test_a_benchmark_task_never_loads_the_scrubbed_columns(self, db_session, db_session_factory, db_engine):
        repo = await seed_repo(db_session)
        task = await seed_task(
            db_session,
            repo,
            status=TaskStatus.COMPLETED,
            outcome=TaskOutcome.FAILED,
            score_reason="SENTINEL-SCORE",
            patch_diff="SENTINEL-DIFF",
            eval_run_id="r",
            instance_id="i",
            run_index=0,
        )

        response, seen = await fetch(db_engine, db_session_factory, f"/tasks/{task.id}")

        assert response.status_code == 200
        assert "SENTINEL" not in response.text
        # Not read at all, rather than read and then nulled: the oracle text never leaves the database.
        assert not selected(seen, "patch_diff") and not selected(seen, "score_reason")
