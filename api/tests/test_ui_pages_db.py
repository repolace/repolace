"""The HTML pages render for rows in every state, and status and outcome stay two separate things."""

import re
import uuid
from decimal import Decimal

import pytest
from api_support import seed_repo, seed_task
from repolace_shared.db.models import TaskOutcome, TaskStatus
from ui_support import at, seed_call, seed_run, seed_stage, ui_client

pytestmark = [pytest.mark.anyio, pytest.mark.db]

NOW_OFFSET = 0


async def page(session_factory, path: str, **kwargs):
    async with ui_client(session_factory) as client:
        return await client.get(path, **kwargs)


def badge_texts(html: str) -> list[str]:
    return re.findall(r'<span class="badge b-[a-z_]+">(?:<svg.*?</svg>)\s*([a-z_]+)</span>', html, flags=re.DOTALL)


def chip_classes(html: str) -> list[str]:
    return re.findall(r'<span class="chip (c-[a-z]+)"', html)


class TestRoot:
    async def test_the_root_redirects_to_the_repo_list(self, db_session_factory):
        async with ui_client(db_session_factory) as client:
            response = await client.get("/")
        assert response.status_code == 307
        assert response.headers["location"] == "/ui/repos"

    async def test_the_repo_list_links_to_tasks_and_new_task(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        html = (await page(db_session_factory, "/ui/repos")).text
        assert "acme/sample" in html
        assert f'href="/ui/repos/{repo.id}/tasks"' in html
        assert f'href="/ui/repos/{repo.id}/tasks/new"' in html

    async def test_an_empty_repo_list_says_so(self, db_session_factory):
        assert "No repositories yet" in (await page(db_session_factory, "/ui/repos")).text

    async def test_an_unknown_repo_is_an_html_404(self, db_session_factory):
        response = await page(db_session_factory, f"/ui/repos/{uuid.uuid4()}/tasks")
        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]

    async def test_a_malformed_id_is_an_html_404(self, db_session_factory):
        response = await page(db_session_factory, "/ui/tasks/not-a-uuid")
        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]


class TestEveryStatusAndOutcome:
    async def seed_all(self, db_session):
        repo = await seed_repo(db_session)
        tasks = {}
        for i, status in enumerate(TaskStatus):
            tasks[status] = await seed_task(
                db_session, repo, issue_number=100 + i, status=status, created_at=at(i), started_at=at(i)
            )
        for j, outcome in enumerate(TaskOutcome):
            kwargs = {}
            if outcome is TaskOutcome.PASSED_WITH_TEST_EDIT:
                kwargs["test_edit_justification"] = "the test asserted the old behaviour"
            tasks[outcome] = await seed_task(
                db_session,
                repo,
                issue_number=200 + j,
                status=TaskStatus.COMPLETED,
                outcome=outcome,
                created_at=at(50 + j),
                **kwargs,
            )
        return repo, tasks

    async def test_the_list_renders_every_status_and_every_outcome_as_separate_elements(
        self, db_session, db_session_factory
    ):
        repo, _ = await self.seed_all(db_session)

        html = (await page(db_session_factory, f"/ui/repos/{repo.id}/tasks")).text

        assert set(badge_texts(html)) == {s.value for s in TaskStatus}
        classes = set(chip_classes(html))
        assert classes == {"c-passed", "c-failed", "c-edit", "c-unscored"}
        # `unscored` is a label for NULL; it must not be offered or rendered as an outcome value.
        assert 'class="chip c-unscored"' in html
        # badge (pill) and chip (square) are different elements for the same row
        row = re.search(r'<tr id="task-[^"]+".*?</tr>', html, flags=re.DOTALL).group(0)
        assert 'class="badge ' in row and 'class="chip ' in row

    async def test_each_detail_page_shows_its_status_badge_and_outcome_chip(self, db_session, db_session_factory):
        _, tasks = await self.seed_all(db_session)
        expected_chip = {
            None: "c-unscored",
            TaskOutcome.PASSED: "c-passed",
            TaskOutcome.FAILED: "c-failed",
            TaskOutcome.PASSED_WITH_TEST_EDIT: "c-edit",
        }
        for key, task in tasks.items():
            response = await page(db_session_factory, f"/ui/tasks/{task.id}")
            assert response.status_code == 200
            html = response.text
            assert badge_texts(html)[0] == task.status.value
            assert expected_chip[task.outcome] in chip_classes(html)
            assert "text/html" in response.headers["content-type"], key

    async def test_a_test_edit_pass_shows_approval_state_and_never_offers_approval(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        pending = await seed_task(
            db_session,
            repo,
            issue_number=1,
            status=TaskStatus.PR_OPENED,
            outcome=TaskOutcome.PASSED_WITH_TEST_EDIT,
            test_edit_justification="the old test was wrong",
        )
        approved = await seed_task(
            db_session,
            repo,
            issue_number=2,
            status=TaskStatus.PR_OPENED,
            outcome=TaskOutcome.PASSED_WITH_TEST_EDIT,
            test_edit_justification="the old test was wrong",
            test_edit_approved_by="maintainer-x",
            test_edit_approved_at=at(5),
        )

        waiting = (await page(db_session_factory, f"/ui/tasks/{pending.id}")).text
        done = (await page(db_session_factory, f"/ui/tasks/{approved.id}")).text

        assert "awaiting sign-off" in waiting
        assert "does not count as a pass" in waiting
        assert "Approved by maintainer-x" in done
        for html in (waiting, done):
            assert "the old test was wrong" in html
            assert "<form" not in html  # nothing on the page can submit anything
            assert not re.search(r">\s*Approve\s*<", html)
            assert "c-passed" not in html  # never the pass colour

    async def test_status_filter_and_outcome_filter_including_unscored(self, db_session, db_session_factory):
        repo, _ = await self.seed_all(db_session)

        failed = (await page(db_session_factory, f"/ui/repos/{repo.id}/tasks?status=failed")).text
        assert set(badge_texts(failed)) == {"failed"}

        unscored = (await page(db_session_factory, f"/ui/repos/{repo.id}/tasks?outcome=unscored")).text
        assert set(chip_classes(unscored)) == {"c-unscored"}

        passed = (await page(db_session_factory, f"/ui/repos/{repo.id}/tasks?outcome=passed")).text
        assert set(chip_classes(passed)) == {"c-passed"}

    async def test_an_unknown_status_filter_is_422_not_a_500(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        response = await page(db_session_factory, f"/ui/repos/{repo.id}/tasks?status=merged")
        assert response.status_code == 422


class TestListStates:
    async def test_a_repo_with_no_tasks_has_its_own_copy_and_a_new_task_button(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        html = (await page(db_session_factory, f"/ui/repos/{repo.id}/tasks")).text
        assert "No tasks yet" in html
        assert html.count(f'href="/ui/repos/{repo.id}/tasks/new"') >= 1

    async def test_a_filter_with_no_match_says_which_filter_and_offers_to_clear_it(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        await seed_task(db_session, repo, status=TaskStatus.QUEUED)
        html = (await page(db_session_factory, f"/ui/repos/{repo.id}/tasks?status=failed")).text
        assert "No tasks match" in html and "status=failed" in html
        assert "Clear filters" in html

    async def test_the_status_filter_uses_exactly_the_enum_vocabulary(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        html = (await page(db_session_factory, f"/ui/repos/{repo.id}/tasks")).text
        segs = re.findall(r'<a class="seg" href="[^"]*"(?: aria-current="true")?>([a-z_]+)</a>', html)
        assert segs == ["all", *(s.value for s in TaskStatus)]

    async def test_the_current_filter_is_marked_with_aria_current(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        html = (await page(db_session_factory, f"/ui/repos/{repo.id}/tasks?status=failed")).text
        assert re.findall(r'aria-current="true">([a-z_]+)<', html) == ["failed"]

    async def test_a_row_shows_the_pr_link_cost_and_notes(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        await seed_task(
            db_session,
            repo,
            issue_number=9,
            status=TaskStatus.PR_OPENED,
            outcome=TaskOutcome.FAILED,
            pr_url="https://github.com/acme/sample/pull/77",
            pr_number=77,
            cost_usd=Decimal("1.2345"),
        )
        await seed_task(
            db_session, repo, issue_number=8, status=TaskStatus.COMPLETED, outcome=TaskOutcome.FAILED,
            agent_stop_reason="budget_usd", created_at=at(1),
        )
        await seed_task(
            db_session, repo, issue_number=7, status=TaskStatus.COMPLETED, created_at=at(2),
        )
        html = (await page(db_session_factory, f"/ui/repos/{repo.id}/tasks")).text
        assert 'href="https://github.com/acme/sample/pull/77"' in html
        assert 'rel="noopener noreferrer"' in html
        assert "$1.23" in html
        assert "PR opened despite failing score" in html
        assert '<span class="k">stop</span>budget_usd' in html
        assert "not scored: the agent did not run" in html

    async def test_a_running_row_shows_its_live_stage_and_cost_so_far_from_the_call_log(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, issue_number=5, status=TaskStatus.RUNNING, started_at=at(0))
        await seed_stage(db_session, task, "clone", "ok", 0, 1)
        await seed_stage(db_session, task, "agent", "running", 2, None)
        await seed_call(db_session, task, "0.25")
        await seed_call(db_session, task, "0.50")

        html = (await page(db_session_factory, f"/ui/repos/{repo.id}/tasks")).text

        assert "$0.75" in html and "so far" in html
        assert '<span class="k">stage</span>Edit' in html
        assert "1 running" in html


class TestTaskDetail:
    async def test_a_finished_task_shows_result_diff_runs_and_facts(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(
            db_session,
            repo,
            issue_number=12,
            issue_title="Handle empty config",
            status=TaskStatus.PR_OPENED,
            outcome=TaskOutcome.PASSED,
            agent_stop_reason="submitted",
            score_reason="Every curated test passes.",
            pr_url="https://github.com/acme/sample/pull/5",
            pr_number=5,
            cost_usd=Decimal("1.12"),
            started_at=at(0),
            completed_at=at(4.2),
            patch_diff="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n",
        )
        await seed_run(db_session, task, 0, passed=3, failed=1)
        await seed_run(db_session, task, 1, passed=4, failed=0)

        html = (await page(db_session_factory, f"/ui/tasks/{task.id}")).text

        assert "Handle empty config" in html
        assert "submitted" in html and "The agent declared the fix done." in html
        assert "Every curated test passes." in html
        assert "$1.12" in html and "of $2.00 cap" in html
        assert "acme/sample#5" in html
        assert '<span class="tag">baseline</span>' in html
        assert "all passing" in html and "1 failing" in html
        assert '<details class="diff" hx-disable open>' in html  # pushed, so open
        assert "app.py" in html
        assert task.id.hex[:8] in html
        # no polling on a finished task
        assert "hx-trigger" not in html

    async def test_the_strip_uses_ok_not_passed_and_greys_plan(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(
            db_session, repo, status=TaskStatus.PR_OPENED, outcome=TaskOutcome.FAILED, started_at=at(0), completed_at=at(9)
        )
        for i, stage in enumerate(("clone", "index", "retrieve", "verify_baseline", "agent", "review", "squash", "push", "pr")):
            await seed_stage(db_session, task, stage, "ok", i, i + 1)

        html = (await page(db_session_factory, f"/ui/tasks/{task.id}")).text
        strip = re.search(r'<ol class="strip">.*?</section>', html, flags=re.DOTALL).group(0)

        assert 'st-ok' in strip
        assert "<b>passed</b>" not in strip  # `passed` is reserved for the outcome
        assert re.search(r'st-skipped.*?<b>skipped</b> &middot; not used yet', strip, flags=re.DOTALL)
        assert strip.count("st-skipped") == 1
        for label in ("Retrieve", "Plan", "Edit", "Test", "Review", "Push", "PR"):
            assert label in strip

    async def test_a_running_task_polls_and_a_running_fragment_keeps_polling(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING, started_at=at(0))
        await seed_stage(db_session, task, "clone", "ok", 0, 1)
        await seed_stage(db_session, task, "index", "running", 1, None)

        full = await page(db_session_factory, f"/ui/tasks/{task.id}")
        fragment = await page(db_session_factory, f"/ui/tasks/{task.id}/live")

        assert f'hx-get="/ui/tasks/{task.id}/live"' in full.text
        assert 'hx-trigger="every 3s"' in full.text
        assert 'aria-live' not in full.text.split('id="live"')[1].split('>')[0]  # polled regions are not live regions
        assert fragment.status_code == 200
        assert "st-running" in fragment.text
        assert "Running: Retrieve" in fragment.text
        assert "<html" not in fragment.text  # a fragment, not a page

    @pytest.mark.parametrize("status", [s for s in TaskStatus if s not in (TaskStatus.QUEUED, TaskStatus.RUNNING)])
    async def test_the_polling_stops_once_the_task_is_terminal(self, db_session, db_session_factory, status):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=status, started_at=at(0), completed_at=at(1))

        full = await page(db_session_factory, f"/ui/tasks/{task.id}")
        fragment = await page(db_session_factory, f"/ui/tasks/{task.id}/live")
        row = await page(db_session_factory, f"/ui/tasks/{task.id}/row")

        assert "hx-trigger" not in full.text
        assert fragment.status_code == 286  # htmx stops polling on 286
        assert "hx-trigger" not in fragment.text
        assert "hx-trigger" not in row.text

    async def test_a_queued_task_polls_and_says_it_is_waiting(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.QUEUED)
        full = await page(db_session_factory, f"/ui/tasks/{task.id}")
        assert 'hx-trigger="every 3s"' in full.text
        assert "waiting for a worker" in full.text

    async def test_a_live_row_polls_itself_and_stops_when_it_finishes(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING, started_at=at(0))

        live = (await page(db_session_factory, f"/ui/tasks/{task.id}/row")).text
        assert f'hx-get="/ui/tasks/{task.id}/row"' in live and 'hx-trigger="every 3s"' in live
        assert 'hx-swap="outerHTML"' in live

        task.status = TaskStatus.COMPLETED
        task.outcome = TaskOutcome.FAILED
        await db_session.commit()
        done = (await page(db_session_factory, f"/ui/tasks/{task.id}/row")).text
        assert "hx-trigger" not in done and "completed" in done

    async def test_a_running_task_shows_cost_so_far_and_the_elapsed_time(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING, started_at=at(0))
        await seed_stage(db_session, task, "agent", "running", 0, None)
        await seed_call(db_session, task, "0.40")

        html = (await page(db_session_factory, f"/ui/tasks/{task.id}/live")).text

        assert "$0.40" in html and "so far" in html
        assert re.search(r"<b>running</b> &middot; (?:<span aria-hidden=.true.>)?\d+:\d\d", html)

    async def test_a_failed_task_shows_the_error_block_the_failed_stage_and_an_aborted_run(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        task = await seed_task(
            db_session,
            repo,
            status=TaskStatus.FAILED,
            error_message="SandboxError: container exited 137\nstage: verify_baseline",
            started_at=at(0),
            completed_at=at(3),
        )
        await seed_stage(db_session, task, "clone", "ok", 0, 1)
        await seed_stage(db_session, task, "index", "ok", 1, 2)
        await seed_stage(db_session, task, "retrieve", "ok", 2, 2.5)
        await seed_stage(
            db_session, task, "verify_baseline", "failed", 2.5, 3, error="SandboxError: container exited 137"
        )
        await seed_run(db_session, task, 0, error="container exited 137", seconds=4)

        html = (await page(db_session_factory, f"/ui/tasks/{task.id}")).text

        assert "Failed in Test: SandboxError: container exited 137" in html
        assert "repolace itself broke" in html
        assert "<pre class=\"msg\">SandboxError: container exited 137\nstage: verify_baseline</pre>" in html
        assert "<dt>Failed stage</dt>" in html
        assert "run aborted" in html
        assert "The agent never stopped on its own" in html
        assert "None. Nothing was pushed." in html
        strip = re.search(r'<ol class="strip">.*?</section>', html, flags=re.DOTALL).group(0)
        assert "st-failed" in strip
        assert strip.count("not reached") == 3  # Edit, Review... Push and PR follow the failed Test node
        assert "c-unscored" in html

    async def test_an_unpushed_diff_is_collapsed_and_tagged(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(
            db_session, repo, status=TaskStatus.COMPLETED, outcome=TaskOutcome.FAILED,
            patch_diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
        )
        html = (await page(db_session_factory, f"/ui/tasks/{task.id}")).text
        assert '<details class="diff" hx-disable>' in html
        assert 'class="tag">not pushed' in html

    async def test_an_empty_diff_says_no_changes_and_a_null_diff_shows_no_panel(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        empty = await seed_task(db_session, repo, status=TaskStatus.COMPLETED, patch_diff="", issue_number=1)
        never = await seed_task(db_session, repo, status=TaskStatus.COMPLETED, patch_diff=None, issue_number=2)
        assert "No changes." in (await page(db_session_factory, f"/ui/tasks/{empty.id}")).text
        assert "No changes." not in (await page(db_session_factory, f"/ui/tasks/{never.id}")).text

    async def test_stop_reasons_each_have_a_gloss(self, db_session, db_session_factory):
        from repolace_shared.db.models import AGENT_STOP_REASONS

        repo = await seed_repo(db_session)
        for reason in AGENT_STOP_REASONS:
            task = await seed_task(
                db_session, repo, status=TaskStatus.COMPLETED, outcome=TaskOutcome.FAILED, agent_stop_reason=reason
            )
            html = (await page(db_session_factory, f"/ui/tasks/{task.id}")).text
            assert f'<span class="mono">{reason}</span><span class="gloss">' in html
            assert '<span class="gloss"></span>' not in html

    async def test_a_stale_heartbeat_on_a_running_task_is_flagged(self, db_session, db_session_factory):
        from datetime import UTC, datetime, timedelta

        repo = await seed_repo(db_session)
        old = datetime.now(UTC) - timedelta(minutes=10)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING, started_at=old, heartbeat_at=old)
        assert "no heartbeat for" in (await page(db_session_factory, f"/ui/tasks/{task.id}")).text
