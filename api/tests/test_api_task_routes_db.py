"""`GET /repos/{id}/tasks`, `GET /tasks/{id}` and `GET /repos/{id}/tasks/estimate`."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from api_support import FakeGithub, client_for, seed_repo, seed_task
from repolace_shared.db.models import LLMCall, TaskOutcome, TaskStage, TaskStatus, TaskTestRun

pytestmark = [pytest.mark.anyio, pytest.mark.db]

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


async def get(db_session_factory, path: str, **params):
    async with client_for(db_session_factory, FakeGithub()) as client:
        return await client.get(path, params=params)


class TestTaskList:
    async def seed_ten(self, db_session):
        repo = await seed_repo(db_session)
        for i in range(10):
            await seed_task(db_session, repo, issue_number=i + 1, created_at=at(i))
        return repo

    async def test_an_unknown_repo_is_404(self, db_session_factory):
        assert (await get(db_session_factory, f"/repos/{uuid.uuid4()}/tasks")).status_code == 404

    async def test_items_carry_the_fields_the_list_needs_newest_first(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        await seed_task(
            db_session,
            repo,
            issue_number=3,
            issue_title="Boom",
            status=TaskStatus.PR_OPENED,
            outcome=TaskOutcome.FAILED,
            agent_stop_reason="max_attempts",
            cost_usd=Decimal("0.4321"),
            pr_url="https://github.com/acme/sample/pull/9",
            pr_number=9,
            created_at=at(1),
            started_at=at(2),
            completed_at=at(3),
        )
        await seed_task(db_session, repo, issue_number=4, created_at=at(5))

        body = (await get(db_session_factory, f"/repos/{repo.id}/tasks")).json()

        assert [i["issue_number"] for i in body["items"]] == [4, 3]
        assert body["next_before"] is None
        old = body["items"][1]
        assert old["issue_title"] == "Boom"
        assert (old["status"], old["outcome"]) == ("pr_opened", "failed")
        assert old["agent_stop_reason"] == "max_attempts"
        assert Decimal(str(old["cost_usd"])) == Decimal("0.4321")
        assert (old["pr_number"], old["pr_url"]) == (9, "https://github.com/acme/sample/pull/9")
        assert old["started_at"] and old["completed_at"] and old["created_at"]
        assert old["error_message"] is None and old["target_branch"] == "main"

    async def test_pages_walk_the_whole_list_with_no_overlap_and_no_gap(self, db_session, db_session_factory):
        repo = await self.seed_ten(db_session)
        seen: list[int] = []
        before = None
        pages = 0
        while True:
            params = {"limit": 4, **({"before": before} if before else {})}
            body = (await get(db_session_factory, f"/repos/{repo.id}/tasks", **params)).json()
            seen += [i["issue_number"] for i in body["items"]]
            pages += 1
            before = body["next_before"]
            if before is None:
                break

        assert seen == list(range(10, 0, -1))
        assert pages == 3

    async def test_an_exact_multiple_of_the_limit_has_no_phantom_next_page(self, db_session, db_session_factory):
        repo = await self.seed_ten(db_session)

        body = (await get(db_session_factory, f"/repos/{repo.id}/tasks", limit=10)).json()

        assert len(body["items"]) == 10
        assert body["next_before"] is None

    async def test_the_cursor_is_the_last_items_created_at_and_is_exclusive(self, db_session, db_session_factory):
        repo = await self.seed_ten(db_session)

        first = (await get(db_session_factory, f"/repos/{repo.id}/tasks", limit=3)).json()
        assert first["next_before"] == first["items"][-1]["created_at"]

        second = (await get(db_session_factory, f"/repos/{repo.id}/tasks", before=first["next_before"])).json()
        assert [i["issue_number"] for i in second["items"]] == [7, 6, 5, 4, 3, 2, 1]

    @pytest.mark.parametrize("limit", [0, 101, -1])
    async def test_limit_outside_1_to_100_is_422(self, db_session, db_session_factory, limit):
        repo = await seed_repo(db_session)

        assert (await get(db_session_factory, f"/repos/{repo.id}/tasks", limit=limit)).status_code == 422

    async def test_a_before_without_a_timezone_is_422(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)

        response = await get(db_session_factory, f"/repos/{repo.id}/tasks", before="2026-10-01T12:00:00")

        assert response.status_code == 422

    async def test_status_and_outcome_filters_combine(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        await seed_task(db_session, repo, issue_number=1, status=TaskStatus.COMPLETED, outcome=TaskOutcome.PASSED)
        await seed_task(db_session, repo, issue_number=2, status=TaskStatus.COMPLETED, outcome=TaskOutcome.FAILED)
        await seed_task(db_session, repo, issue_number=3, status=TaskStatus.PR_OPENED, outcome=TaskOutcome.PASSED)

        async def numbers(**params):
            body = (await get(db_session_factory, f"/repos/{repo.id}/tasks", **params)).json()
            return sorted(i["issue_number"] for i in body["items"])

        assert await numbers(status="completed") == [1, 2]
        assert await numbers(outcome="passed") == [1, 3]
        assert await numbers(status="completed", outcome="passed") == [1]

    @pytest.mark.parametrize("param", ["status", "outcome"])
    async def test_an_unknown_enum_value_is_422(self, db_session, db_session_factory, param):
        repo = await seed_repo(db_session)

        response = await get(db_session_factory, f"/repos/{repo.id}/tasks", **{param: "merged"})

        assert response.status_code == 422

    async def test_benchmark_tasks_are_hidden_unless_asked_for(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        await seed_task(db_session, repo, issue_number=1)
        await seed_task(db_session, repo, issue_number=2, eval_run_id="run-1", instance_id="x__y-1", run_index=0)

        async def numbers(**params):
            body = (await get(db_session_factory, f"/repos/{repo.id}/tasks", **params)).json()
            return sorted(i["issue_number"] for i in body["items"])

        assert await numbers() == [1]
        assert await numbers(include_benchmark="true") == [1, 2]


class TestTaskDetail:
    async def test_an_unknown_task_is_404(self, db_session_factory):
        assert (await get(db_session_factory, f"/tasks/{uuid.uuid4()}")).status_code == 404

    async def test_a_malformed_id_is_422(self, db_session_factory):
        assert (await get(db_session_factory, "/tasks/not-a-uuid")).status_code == 422

    async def test_the_full_row_with_stages_and_test_run_counts(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(
            db_session,
            repo,
            status=TaskStatus.COMPLETED,
            outcome=TaskOutcome.PASSED_WITH_TEST_EDIT,
            test_edit_justification="the test was wrong",
            test_edit_approved_by="rounak",
            test_edit_approved_at=at(9),
            patch_diff="diff --git a/x b/x\n",
            score_reason="every curated test passes",
            retry_count=1,
            claimed_by="host:123",
            heartbeat_at=at(8),
            started_at=at(1),
        )
        # Inserted out of order: the route, not insertion order, sorts by started_at.
        db_session.add_all(
            [
                TaskStage(task_id=task.id, stage="agent", status="failed", started_at=at(3), ended_at=at(4), error="no"),
                TaskStage(task_id=task.id, stage="clone", status="ok", started_at=at(1), ended_at=at(2)),
                TaskStage(task_id=task.id, stage="index", status="running", started_at=at(2)),
            ]
        )
        for attempt, passed, failed, error in [(0, 5, 2, None), (2, 7, 0, "timeout")]:
            db_session.add(
                TaskTestRun(
                    task_id=task.id,
                    attempt=attempt,
                    commit_sha="abc",
                    passed=[f"t{i}" for i in range(passed)],
                    failed=[f"f{i}" for i in range(failed)],
                    xfailed=["x"],
                    skipped=["s1", "s2"],
                    collect_failures=["c"] if attempt else [],
                    error=error,
                    duration_seconds=Decimal("1.5"),
                )
            )
        await db_session.commit()

        body = (await get(db_session_factory, f"/tasks/{task.id}")).json()

        assert body["status"] == "completed" and body["outcome"] == "passed_with_test_edit"
        assert body["attempts"] == 2
        assert body["retry_count"] == 1
        assert body["patch_diff"] == "diff --git a/x b/x\n"
        assert body["score_reason"] == "every curated test passes"
        assert body["test_edit_justification"] == "the test was wrong"
        assert body["test_edit_approved_by"] == "rounak"
        assert body["test_edit_approved_at"].startswith("2026-10-01T12:09")
        assert body["claimed_by"] == "host:123"
        assert body["heartbeat_at"].startswith("2026-10-01T12:08")
        assert [(s["stage"], s["status"]) for s in body["stages"]] == [
            ("clone", "ok"),
            ("index", "running"),
            ("agent", "failed"),
        ]
        assert body["stages"][2]["error"] == "no"
        assert body["stages"][1]["ended_at"] is None
        runs = body["test_runs"]
        assert [r["attempt"] for r in runs] == [0, 2]
        assert [(r["passed"], r["failed"], r["xfailed"], r["skipped"]) for r in runs] == [(5, 2, 1, 2), (7, 0, 1, 2)]
        assert [(r["collect_failures"], r["error"]) for r in runs] == [(0, False), (1, True)]
        assert Decimal(str(runs[0]["duration_seconds"])) == Decimal("1.5")

    async def test_node_ids_never_appear_in_the_response(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo)
        db_session.add(
            TaskTestRun(task_id=task.id, attempt=0, commit_sha="a", failed=["tests/test_secret.py::test_hidden"])
        )
        await db_session.commit()

        text = (await get(db_session_factory, f"/tasks/{task.id}")).text

        assert "test_hidden" not in text

    async def test_a_task_with_no_runs_or_stages(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo)

        body = (await get(db_session_factory, f"/tasks/{task.id}")).json()

        assert (body["attempts"], body["stages"], body["test_runs"]) == (0, [], [])
        assert body["outcome"] is None and body["claimed_by"] is None and body["heartbeat_at"] is None


class TestEstimate:
    async def costed(self, db_session, repo, costs, *, model=None):
        for i, cost in enumerate(costs):
            task = await seed_task(db_session, repo, issue_number=i + 1, cost_usd=cost, created_at=at(i))
            if model:
                db_session.add(
                    LLMCall(task_id=task.id, stage="agent", model=model, provider="anthropic", created_at=at(i))
                )
                await db_session.commit()

    async def estimate(self, db_session_factory, repo):
        response = await get(db_session_factory, f"/repos/{repo.id}/tasks/estimate")
        assert response.status_code == 200
        return response.json()

    async def test_an_unknown_repo_is_404(self, db_session_factory):
        assert (await get(db_session_factory, f"/repos/{uuid.uuid4()}/tasks/estimate")).status_code == 404

    async def test_basis_repo_uses_this_repos_recent_percentiles(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        await self.costed(db_session, repo, [Decimal(c) for c in ("0.10", "0.20", "0.30", "0.40", "1.00")])

        body = await self.estimate(db_session_factory, repo)

        assert body["basis"] == "repo" and body["n"] == 5
        assert Decimal(str(body["p50_usd"])) == Decimal("0.3")
        assert Decimal(str(body["p90_usd"])) == Decimal("0.76")  # 0.4 + 0.6 * 0.6, linear interpolation
        assert Decimal(str(body["upper_bound_usd"])) == Decimal("2.00")

    async def test_only_the_last_30_count_and_benchmark_and_unpriced_tasks_do_not(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        # The 5 oldest are expensive and fall outside the window of 30.
        await self.costed(db_session, repo, [Decimal("9.00")] * 5 + [Decimal("0.10")] * 30)
        await seed_task(
            db_session,
            repo,
            issue_number=99,
            cost_usd=Decimal(7),
            created_at=at(500),
            eval_run_id="r",
            instance_id="i",
            run_index=0,
        )
        await seed_task(db_session, repo, issue_number=98, cost_usd=None, created_at=at(501))

        body = await self.estimate(db_session_factory, repo)

        assert (body["basis"], body["n"]) == ("repo", 30)
        assert Decimal(str(body["p90_usd"])) == Decimal("0.1")

    async def test_basis_model_falls_back_to_other_repos_on_the_same_model(self, db_session, db_session_factory):
        mine = await seed_repo(db_session)
        other = await seed_repo(db_session, id=uuid.uuid4(), github_repo_id=100, name="other", full_name="acme/other")
        await self.costed(db_session, mine, [Decimal("0.50")], model="m1")
        # Another repo: 4 tasks on m1 (so 5 in all) and 3 on a different model, which must not count.
        for i, (cost, model) in enumerate([("0.10", "m1")] * 4 + [("5.00", "m2")] * 3):
            task = await seed_task(db_session, other, issue_number=i + 1, cost_usd=Decimal(cost), created_at=at(i))
            db_session.add(LLMCall(task_id=task.id, stage="agent", model=model, provider="p", created_at=at(i)))
        await db_session.commit()

        body = await self.estimate(db_session_factory, mine)

        assert (body["basis"], body["n"]) == ("model", 5)
        assert Decimal(str(body["p50_usd"])) == Decimal("0.1")

    async def test_basis_none_below_five_even_with_a_model(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        await self.costed(db_session, repo, [Decimal("0.50")] * 4, model="m1")

        body = await self.estimate(db_session_factory, repo)

        assert body["basis"] == "none"
        assert body["n"] == 4
        assert body["p50_usd"] is None and body["p90_usd"] is None
        assert Decimal(str(body["upper_bound_usd"])) == Decimal("2.00")

    async def test_basis_none_for_a_repo_with_no_tasks(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)

        body = await self.estimate(db_session_factory, repo)

        assert (body["basis"], body["n"], body["p50_usd"]) == ("none", 0, None)

    async def test_the_estimate_route_is_not_swallowed_by_the_task_list(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)

        body = await self.estimate(db_session_factory, repo)

        assert "basis" in body
