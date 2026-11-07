"""The monthly allowance on the enqueue path (JSON and UI), and that nothing changes when sign-in is
not configured or nobody is signed in."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from api_billing_support import SAME_ORIGIN, seed_subscription, seed_user, session_cookie, settings
from api_support import FakeGithub, raw_issue, seed_repo, seed_task
from repolace_api.billing_limits import FREE_TASKS_PER_MONTH, PRO_TASKS_PER_MONTH, month_start
from repolace_api.config import get_billing_settings
from repolace_shared.db.models import Task
from sqlalchemy import select
from ui_support import ui_app

pytestmark = [pytest.mark.anyio, pytest.mark.db]


def client(session_factory, *, billing=None, cookies=None, github=None) -> httpx.AsyncClient:
    app = ui_app(session_factory, github or FakeGithub(*[raw_issue(n) for n in range(1, 400)]))
    app.dependency_overrides[get_billing_settings] = lambda: billing or settings()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies)


async def fill(session, repo, user, n: int, **overrides):
    for i in range(n):
        await seed_task(session, repo, issue_number=1000 + i, user_id=user.id, **overrides)


class TestJsonEnqueue:
    async def test_a_signed_in_task_records_its_owner(self, db_session, db_session_factory):
        repo, user = await seed_repo(db_session), await seed_user(db_session)
        async with client(db_session_factory, cookies=session_cookie(user.id)) as c:
            response = await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7})
        assert response.status_code == 201
        task = (await db_session.execute(select(Task))).scalar_one()
        assert task.user_id == user.id

    async def test_the_free_allowance_refuses_with_402(self, db_session, db_session_factory):
        repo, user = await seed_repo(db_session), await seed_user(db_session)
        await fill(db_session, repo, user, FREE_TASKS_PER_MONTH)
        async with client(db_session_factory, cookies=session_cookie(user.id)) as c:
            response = await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7})
        assert response.status_code == 402
        assert "free plan" in response.json()["detail"] and str(FREE_TASKS_PER_MONTH) in response.json()["detail"]
        assert len((await db_session.execute(select(Task))).scalars().all()) == FREE_TASKS_PER_MONTH

    async def test_one_under_the_limit_is_allowed(self, db_session, db_session_factory):
        repo, user = await seed_repo(db_session), await seed_user(db_session)
        await fill(db_session, repo, user, FREE_TASKS_PER_MONTH - 1)
        async with client(db_session_factory, cookies=session_cookie(user.id)) as c:
            assert (await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7})).status_code == 201

    async def test_pro_has_the_larger_allowance(self, db_session, db_session_factory):
        repo, user = await seed_repo(db_session), await seed_user(db_session)
        await seed_subscription(db_session, user, plan="pro", status="active")
        await fill(db_session, repo, user, FREE_TASKS_PER_MONTH + 1)
        async with client(db_session_factory, cookies=session_cookie(user.id)) as c:
            assert (await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7})).status_code == 201
        assert PRO_TASKS_PER_MONTH > FREE_TASKS_PER_MONTH

    async def test_benchmark_other_users_and_last_month_do_not_count(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        user, other = await seed_user(db_session), await seed_user(db_session, 2002, "other")
        await fill(db_session, repo, other, FREE_TASKS_PER_MONTH)
        for i in range(FREE_TASKS_PER_MONTH):
            await seed_task(
                db_session, repo, issue_number=2000 + i, user_id=user.id, eval_run_id="run", instance_id=f"i{i}", run_index=i
            )
        for i in range(FREE_TASKS_PER_MONTH):
            await seed_task(
                db_session, repo, issue_number=3000 + i, user_id=user.id,
                created_at=month_start(datetime.now(UTC)) - timedelta(days=1),
            )
        async with client(db_session_factory, cookies=session_cookie(user.id)) as c:
            assert (await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7})).status_code == 201

    async def test_the_limit_is_checked_after_the_cheap_validations(self, db_session, db_session_factory):
        repo, user = await seed_repo(db_session), await seed_user(db_session)
        await fill(db_session, repo, user, FREE_TASKS_PER_MONTH)
        async with client(db_session_factory, cookies=session_cookie(user.id)) as c:
            missing = await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": 99999})
        assert missing.status_code == 404


class TestNothingChangesWhenUnconfigured:
    @pytest.mark.parametrize(
        "billing",
        [
            settings(session_secret=None),
            settings(github_oauth_client_id=None),
            settings(github_oauth_client_secret=None),
            settings(session_secret="short"),
        ],
    )
    async def test_no_signin_means_no_owner_and_no_limit(self, db_session, db_session_factory, billing):
        repo, user = await seed_repo(db_session), await seed_user(db_session)
        await fill(db_session, repo, user, FREE_TASKS_PER_MONTH + 3)
        async with client(db_session_factory, billing=billing, cookies=session_cookie(user.id)) as c:
            response = await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7})
        assert response.status_code == 201
        newest = (await db_session.execute(select(Task).where(Task.issue_number == 7))).scalar_one()
        assert newest.user_id is None

    async def test_nobody_signed_in_means_no_owner_and_no_limit(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        async with client(db_session_factory) as c:
            for n in range(1, FREE_TASKS_PER_MONTH + 3):
                assert (await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": n})).status_code == 201
        owners = (await db_session.execute(select(Task.user_id))).scalars().all()
        assert set(owners) == {None}

    async def test_a_deleted_user_keeps_the_task(self, db_session, db_session_factory):
        repo, user = await seed_repo(db_session), await seed_user(db_session)
        task = await seed_task(db_session, repo, user_id=user.id)
        await db_session.delete(user)
        await db_session.commit()
        await db_session.refresh(task)
        assert task.user_id is None


class TestUiEnqueue:
    async def test_over_the_limit_is_shown_as_the_form_error(self, db_session, db_session_factory):
        repo, user = await seed_repo(db_session), await seed_user(db_session)
        await fill(db_session, repo, user, FREE_TASKS_PER_MONTH)
        async with client(db_session_factory, cookies=session_cookie(user.id)) as c:
            response = await c.post(f"/ui/repos/{repo.id}/tasks", data={"issue_number": "7"}, headers=SAME_ORIGIN)
        assert response.status_code == 402
        assert 'id="form-error"' in response.text and "free plan allows" in response.text

    async def test_under_the_limit_redirects_and_records_the_owner(self, db_session, db_session_factory):
        repo, user = await seed_repo(db_session), await seed_user(db_session)
        async with client(db_session_factory, cookies=session_cookie(user.id)) as c:
            response = await c.post(f"/ui/repos/{repo.id}/tasks", data={"issue_number": "7"}, headers=SAME_ORIGIN)
        assert response.status_code == 303
        assert (await db_session.execute(select(Task.user_id))).scalar_one() == user.id

    async def test_unconfigured_ui_is_unmetered(self, db_session, db_session_factory):
        repo, user = await seed_repo(db_session), await seed_user(db_session)
        await fill(db_session, repo, user, FREE_TASKS_PER_MONTH)
        async with client(db_session_factory, billing=settings(session_secret=None), cookies=session_cookie(user.id)) as c:
            response = await c.post(f"/ui/repos/{repo.id}/tasks", data={"issue_number": "7"}, headers=SAME_ORIGIN)
        assert response.status_code == 303
