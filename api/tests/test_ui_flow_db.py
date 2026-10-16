"""The enqueue flow, list paging fragments and the JSON additions the pages depend on."""

import re
from decimal import Decimal

import httpx
import pytest
from api_support import FakeGithub, raw_issue, seed_repo, seed_task
from repolace_shared.db.models import Task, TaskOutcome, TaskStatus
from sqlalchemy import select
from ui_support import SAME_ORIGIN, at, seed_call, ui_client

pytestmark = [pytest.mark.anyio, pytest.mark.db]


def github(*issues, **kwargs) -> FakeGithub:
    return FakeGithub(*(issues or (raw_issue(7), raw_issue(8, title="Another bug"))), **kwargs)


class FailingGithub(FakeGithub):
    async def list_repo_open_issues(self, installation_id, owner, repo):
        raise httpx.ConnectError("boom")


class TestNewTaskPage:
    async def test_the_page_has_the_three_steps_the_issues_and_the_default_branch_preselected(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        async with ui_client(db_session_factory, github()) as client:
            html = (await client.get(f"/ui/repos/{repo.id}/tasks/new")).text

        assert "Pick an open issue" in html and "Target branch" in html and "Confirm" in html
        assert 'name="issue_number" value="7"' in html and 'name="issue_number" value="8"' in html
        assert "parse_config crashes" in html and "Another bug" in html
        assert '<option value="main" selected>main (default)</option>' in html
        assert '<option value="develop">develop</option>' in html
        assert "2 open" in html
        # no issue picked yet: the start button is disabled and the hint says why
        assert re.search(r'<button type="submit" class="btn btn-primary" aria-describedby="pick-hint" disabled>', html)
        assert "Pick an issue in step 1" in html
        assert 'hx-trigger="input changed delay:250ms, search"' in html
        assert 'aria-live="polite"' in html
        assert "Hard stop at $2.00" in html

    async def test_the_issue_search_filters_the_cached_list_without_another_github_call(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        fake = github()
        async with ui_client(db_session_factory, fake) as client:
            await client.get(f"/ui/repos/{repo.id}/tasks/new")
            calls_after_page = len(fake.calls)
            by_title = (await client.get(f"/ui/repos/{repo.id}/tasks/new/issues?q=another")).text
            by_number = (await client.get(f"/ui/repos/{repo.id}/tasks/new/issues?q=%237")).text
            none = (await client.get(f"/ui/repos/{repo.id}/tasks/new/issues?q=zzz")).text
            kept = (await client.get(f"/ui/repos/{repo.id}/tasks/new/issues?q=&issue_number=8")).text

        assert len(fake.calls) == calls_after_page == 1  # one GitHub call, then the cache
        assert "Another bug" in by_title and "parse_config" not in by_title
        assert "parse_config" in by_number and "Another bug" not in by_number
        assert 'No open issues match <q>zzz</q>.' in none
        assert 'value="8" checked' in kept  # the pick survives a re-filter

    async def test_the_issue_cache_expires(self, db_session, db_session_factory, monkeypatch):
        import time

        repo = await seed_repo(db_session)
        fake = github()
        real = time.monotonic
        offset = {"value": 0.0}
        monkeypatch.setattr(time, "monotonic", lambda: real() + offset["value"])
        async with ui_client(db_session_factory, fake) as client:
            await client.get(f"/ui/repos/{repo.id}/tasks/new/issues")
            await client.get(f"/ui/repos/{repo.id}/tasks/new/issues")
            assert len(fake.calls) == 1
            offset["value"] = 61.0
            await client.get(f"/ui/repos/{repo.id}/tasks/new/issues")
        assert len(fake.calls) == 2

    async def test_the_confirm_fragment_enables_start_and_names_the_target_branch(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        async with ui_client(db_session_factory, github()) as client:
            html = (await client.get(f"/ui/repos/{repo.id}/tasks/new/confirm?issue_number=7&target_branch=develop")).text

        assert "will target" in html and "develop" in html
        assert "parse_config crashes" in html
        assert "disabled" not in html
        assert "<html" not in html

    async def test_a_stale_issue_number_is_called_out_not_silently_ignored(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        async with ui_client(db_session_factory, github()) as client:
            html = (await client.get(f"/ui/repos/{repo.id}/tasks/new/confirm?issue_number=999")).text
        assert "Issue #999 is not in the current list" in html and "disabled" in html

    async def test_a_github_failure_is_shown_in_the_page_not_as_a_crash(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        async with ui_client(db_session_factory, FailingGithub()) as client:
            response = await client.get(f"/ui/repos/{repo.id}/tasks/new")
        assert response.status_code == 200
        assert "Could not load open issues" in response.text
        assert 'role="alert"' in response.text

    async def test_an_issue_list_with_no_issues_says_so(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        async with ui_client(db_session_factory, FakeGithub()) as client:
            html = (await client.get(f"/ui/repos/{repo.id}/tasks/new")).text
        assert "This repository has no open issues." in html


class TestEstimatePanel:
    async def confirm(self, session_factory, repo) -> str:
        async with ui_client(session_factory, github()) as client:
            return (await client.get(f"/ui/repos/{repo.id}/tasks/new/confirm")).text

    async def test_no_history_is_the_empty_state_with_the_cap(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        html = await self.confirm(db_session_factory, repo)
        assert "No estimate yet" in html
        assert "No history yet, capped at $2.00." in html
        assert "low history" in html
        assert "<svg class=\"bar\"" in html and 'class="band"' not in html

    async def test_enough_repo_history_shows_the_range_the_basis_and_the_band(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        for i, cost in enumerate(("0.50", "0.60", "0.70", "0.80", "1.00", "1.20")):
            await seed_task(
                db_session, repo, issue_number=i + 1, status=TaskStatus.COMPLETED, cost_usd=Decimal(cost), created_at=at(i)
            )
        html = await self.confirm(db_session_factory, repo)
        assert "Median of the last 6 tasks on this repo." in html
        assert re.search(r"\$0\.\d\d &ndash; \$1\.\d\d", html) or re.search(r"\$0\.\d\d – \$1\.\d\d", html)
        assert "low history" not in html
        assert 'class="band"' in html

    async def test_a_model_basis_is_worded_as_other_repos_and_flagged(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        other = await seed_repo(db_session, github_repo_id=100, name="other", full_name="acme/other")
        mine = await seed_task(db_session, repo, status=TaskStatus.COMPLETED, cost_usd=Decimal("0.9"), created_at=at(10))
        await seed_call(db_session, mine, "0.9", model="shared-model")
        for i in range(5):
            t = await seed_task(
                db_session, other, issue_number=i + 1, status=TaskStatus.COMPLETED, cost_usd=Decimal("1.0"), created_at=at(i)
            )
            await seed_call(db_session, t, "1.0", model="shared-model")
        html = await self.confirm(db_session_factory, repo)
        assert "tasks on the same model, across repos" in html
        assert "low history" in html

    async def test_p90_above_the_cap_is_clipped_and_says_so(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        for i in range(6):
            await seed_task(
                db_session, repo, issue_number=i + 1, status=TaskStatus.COMPLETED, cost_usd=Decimal("3.00"), created_at=at(i)
            )
        html = await self.confirm(db_session_factory, repo)
        assert "clipped to the $2.00 cap" in html


class TestEnqueue:
    async def post(self, session_factory, repo, fake=None, headers=SAME_ORIGIN, **form):
        async with ui_client(session_factory, fake or github()) as client:
            return await client.post(
                f"/ui/repos/{repo.id}/tasks", data=form, headers=headers, follow_redirects=False
            )

    async def test_the_post_creates_the_task_through_the_json_routes_function_and_redirects(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)

        response = await self.post(db_session_factory, repo, issue_number="7", target_branch="develop")

        assert response.status_code == 303
        task = (await db_session.execute(select(Task))).scalar_one()
        assert response.headers["location"] == f"/ui/tasks/{task.id}"
        assert (task.issue_number, task.target_branch, task.status) == (7, "develop", TaskStatus.QUEUED)
        assert task.issue_title == "parse_config crashes on an empty config file"  # resolved from GitHub, not the form
        assert task.open_pr_on_failure is False

    async def test_the_redirect_lands_on_a_queued_detail_page(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        async with ui_client(db_session_factory, github()) as client:
            response = await client.post(
                f"/ui/repos/{repo.id}/tasks",
                data={"issue_number": "7", "target_branch": "main"},
                headers=SAME_ORIGIN,
                follow_redirects=True,
            )
        assert response.status_code == 200
        assert "queued" in response.text and "waiting for a worker" in response.text

    async def test_a_blank_branch_falls_back_to_the_default(self, db_session, db_session_factory):
        repo = await seed_repo(db_session, default_branch="trunk")
        await self.post(db_session_factory, repo, issue_number="7", target_branch="")
        task = (await db_session.execute(select(Task))).scalar_one()
        assert task.target_branch == "trunk"

    async def test_a_branch_that_does_not_exist_is_a_422_form_error_with_the_form_kept(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)

        response = await self.post(db_session_factory, repo, issue_number="7", target_branch="no-such-branch")

        assert response.status_code == 422
        html = response.text
        assert re.search(r'id="branch-error" role="alert" tabindex="-1" data-focus-on-load>[^<]*is not a branch of acme/sample', html)
        assert 'aria-invalid="true" aria-describedby="branch-error branch-hint"' in html
        assert "no-such-branch" in html
        assert 'name="issue_number" value="7" checked' in html  # the pick survives
        assert (await db_session.execute(select(Task))).first() is None

    @pytest.mark.parametrize("number", ["", "abc", "0", "-3"])
    async def test_a_missing_or_invalid_issue_is_a_422_form_error(self, db_session, db_session_factory, number):
        repo = await seed_repo(db_session)
        response = await self.post(db_session_factory, repo, issue_number=number, target_branch="main")
        assert response.status_code == 422
        assert 'role="alert"' in response.text
        assert (await db_session.execute(select(Task))).first() is None

    async def test_an_issue_that_is_not_open_is_a_404_form_error(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        response = await self.post(db_session_factory, repo, issue_number="99", target_branch="main")
        assert response.status_code == 404
        assert "issue not found or not open" in response.text

    async def test_a_github_outage_is_a_502_form_error(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        response = await self.post(db_session_factory, repo, fake=FailingGithub(), issue_number="7", target_branch="main")
        assert response.status_code == 502
        assert "GitHub API unreachable" in response.text

    async def test_an_unknown_repo_is_a_404_page(self, db_session_factory):
        import uuid

        async with ui_client(db_session_factory, github()) as client:
            response = await client.post(
                f"/ui/repos/{uuid.uuid4()}/tasks", data={"issue_number": "7"}, headers=SAME_ORIGIN
            )
        assert response.status_code == 404


class TestListPaging:
    async def seed_many(self, session, repo, n=7):
        for i in range(n):
            await seed_task(session, repo, issue_number=i + 1, issue_title=f"issue-{i + 1:02d}", created_at=at(i))

    async def test_load_more_walks_the_list_with_the_opaque_cursor_and_no_overlap(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        await self.seed_many(db_session, repo, 120)
        async with ui_client(db_session_factory) as client:
            first = (await client.get(f"/ui/repos/{repo.id}/tasks")).text
            seen = re.findall(r'<a class="rowlink" id="link-[^"]+" href="/ui/tasks/[^"]+">(issue-\d+)</a>', first)
            assert len(seen) == 50
            fragments = 0
            html = first
            while True:
                match = re.search(r'hx-get="(/ui/repos/[^"]+/tasks/more\?[^"]+)"', html)
                if not match:
                    break
                url = match.group(1).replace("&amp;", "&")
                response = await client.get(url)
                html = response.text
                assert "<html" not in html  # a fragment: rows plus the next load-more
                seen += re.findall(r'<a class="rowlink" id="link-[^"]+" href="/ui/tasks/[^"]+">(issue-\d+)</a>', html)
                fragments += 1

        assert fragments == 2
        assert seen == [f"issue-{i:02d}" for i in range(120, 0, -1)]
        assert "Load more" not in html  # the last page has no button

    async def test_the_load_more_link_keeps_the_filters(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        for i in range(60):
            await seed_task(db_session, repo, issue_number=i + 1, status=TaskStatus.FAILED, created_at=at(i))
        await seed_task(db_session, repo, issue_number=999, status=TaskStatus.QUEUED, created_at=at(100))
        async with ui_client(db_session_factory) as client:
            html = (await client.get(f"/ui/repos/{repo.id}/tasks?status=failed&outcome=unscored")).text
        link = re.search(r'hx-get="([^"]+)"', html.split("Load more")[0][-400:]).group(1)
        assert "status=failed" in link and "outcome=unscored" in link and "cursor=" in link
        assert "#999" not in html

    async def test_a_malformed_cursor_is_a_422_page(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        async with ui_client(db_session_factory) as client:
            response = await client.get(f"/ui/repos/{repo.id}/tasks/more?cursor=!!!")
        assert response.status_code == 422

    async def test_the_filter_form_swaps_the_list_without_a_reload(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        async with ui_client(db_session_factory) as client:
            html = (await client.get(f"/ui/repos/{repo.id}/tasks")).text
        assert 'hx-trigger="change"' in html and 'hx-push-url="true"' in html and 'hx-select="#tasklist"' in html
        assert "all outcomes" in html and ">unscored</option>" in html


class TestJsonAdditions:
    async def test_task_detail_carries_the_repo_full_name_and_is_benchmark(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo)
        async with ui_client(db_session_factory) as client:
            body = (await client.get(f"/tasks/{task.id}")).json()
        assert body["repo_full_name"] == "acme/sample"
        assert body["is_benchmark"] is False
        assert body["cost_so_far_usd"] is None

    async def test_a_live_task_reports_cost_so_far_from_the_call_log_and_a_finished_one_does_not(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        live = await seed_task(db_session, repo, status=TaskStatus.RUNNING)
        done = await seed_task(db_session, repo, status=TaskStatus.COMPLETED, cost_usd=Decimal("0.30"), issue_number=2)
        await seed_call(db_session, live, "0.10")
        await seed_call(db_session, live, "0.15")
        await seed_call(db_session, done, "0.30")
        async with ui_client(db_session_factory) as client:
            live_body = (await client.get(f"/tasks/{live.id}")).json()
            done_body = (await client.get(f"/tasks/{done.id}")).json()
        assert Decimal(str(live_body["cost_so_far_usd"])) == Decimal("0.25")
        assert live_body["cost_usd"] is None
        assert done_body["cost_so_far_usd"] is None

    async def test_the_json_list_nulls_a_benchmark_rows_error_message_and_marks_it(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        await seed_task(
            db_session, repo, status=TaskStatus.FAILED, error_message="SENTINEL sandbox output", eval_run_id="r",
            instance_id="i", run_index=0, outcome=TaskOutcome.FAILED,
        )
        await seed_task(db_session, repo, issue_number=2, status=TaskStatus.FAILED, error_message="plain failure")
        async with ui_client(db_session_factory) as client:
            items = (await client.get(f"/repos/{repo.id}/tasks", params={"include_benchmark": "true"})).json()["items"]
        by_flag = {i["is_benchmark"]: i for i in items}
        assert by_flag[True]["error_message"] is None
        assert by_flag[False]["error_message"] == "plain failure"
