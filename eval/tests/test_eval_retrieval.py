"""`harness.retrieval_eval`: gold targets, planning, the run, and the eval-only rows.

The orchestration is exercised with a fake `RetrievalApi` (no embedding, no model),
over a real git fixture and real tables. The one test that binds to the real
`retrieval` package -- with the shared fake embedder -- needs the strategy-aware
API of stream C and skips, saying so, where that is absent.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from eval_support import (
    MOD,
    TEST_MOD,
    Upstream,
    build_upstream,
    commit_files,
    git_text,
    make_repo,
    patch_from,
    write_instance,
)
from harness import retrieval_eval as re_
from harness.metrics import Span
from harness.retrieval_eval import (
    DEFAULT_MAX_INDEX_EQUIVALENTS,
    EVAL_INSTALLATION_ID,
    STRATEGIES,
    EvalRun,
    GoldTargets,
    Query,
    RepoPlan,
    RetrievalApi,
    RetrievalEvalError,
    RetrievalUnavailable,
    eval_github_repo_id,
    ensure_eval_repo,
    gold_targets,
    load_eval_instances,
    open_git_source,
    plan_repo,
    run_eval,
    summarize,
    to_json,
    to_markdown,
)
from harness.select_instances import CloneCache
from repolace_shared.db.models import CodeChunk, GithubInstallation, RegisteredRepo
from repolace_shared.github.schemas import Repository, RepoOwner
from repolace_shared.instances import load_instances

LINES = [f"l{i}" for i in range(1, 31)]


# --- gold targets ------------------------------------------------------------


def diff(path: str, old: list[str], new: list[str]) -> str:
    import difflib

    return "".join(
        ["diff --git a/{0} b/{0}\n".format(path)]
        + [line if line.endswith("\n") else line + "\n"
           for line in difflib.unified_diff(
               [f"{x}\n" for x in old], [f"{x}\n" for x in new], f"a/{path}", f"b/{path}", lineterm="\n")]
    )


class TestGoldTargets:
    def test_modified_files_and_their_changed_old_lines(self):
        after = list(LINES)
        after[4] = "X5"
        patch = diff("pkg/mod.py", LINES, after) + diff("pkg/other.py", LINES, [*LINES, "tail"])
        gold = gold_targets(patch)
        assert gold.paths == ("pkg/mod.py", "pkg/other.py")
        assert gold.hunks == (Span("pkg/mod.py", 5, 5), Span("pkg/other.py", 30, 30))

    def test_a_file_the_patch_only_adds_is_not_a_target(self):
        added = "diff --git a/pkg/new.py b/pkg/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/pkg/new.py\n@@ -0,0 +1 @@\n+x\n"
        assert gold_targets(added) == GoldTargets((), ())

    def test_a_test_or_config_path_is_dropped_even_if_a_hand_made_patch_has_one(self):
        patch = diff("tests/test_x.py", LINES, [*LINES, "x"]) + diff("pkg/mod.py", LINES, [*LINES, "x"])
        assert gold_targets(patch).paths == ("pkg/mod.py",)

    def test_a_rename_is_keyed_by_the_path_it_has_in_the_index(self):
        rename = (
            "diff --git a/pkg/a.py b/pkg/b.py\nsimilarity index 90%\nrename from pkg/a.py\nrename to pkg/b.py\n"
            "--- a/pkg/a.py\n+++ b/pkg/b.py\n@@ -1 +1 @@\n-x\n+y\n"
        )
        gold = gold_targets(rename)
        assert gold.paths == ("pkg/a.py",) and gold.hunks == (Span("pkg/a.py", 1, 1),)

    def test_a_deleted_file_is_a_target_removed_in_full(self):
        deleted = "diff --git a/pkg/a.py b/pkg/a.py\ndeleted file mode 100644\n--- a/pkg/a.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n"
        assert gold_targets(deleted).hunks == (Span("pkg/a.py", 1, 2),)

    def test_a_path_is_listed_once_however_many_hunks_it_has(self):
        after = list(LINES)
        after[1] = "A"
        after[27] = "B"
        gold = gold_targets(diff("pkg/mod.py", LINES, after))
        assert gold.paths == ("pkg/mod.py",) and len(gold.hunks) == 2


# --- ids ---------------------------------------------------------------------


class TestSyntheticIds:
    def test_negative_stable_and_within_a_signed_64_bit_column(self):
        value = eval_github_repo_id("psf/requests", "truncate")
        assert -(2**63) <= value < 0
        assert value == eval_github_repo_id("psf/requests", "truncate")

    def test_every_repo_and_strategy_gets_its_own_id(self):
        ids = {eval_github_repo_id(repo, strategy) for repo in ("psf/requests", "pallets/flask") for strategy in STRATEGIES}
        assert len(ids) == 8

    def test_it_never_collides_with_a_real_repository_id(self):
        assert all(eval_github_repo_id("a/b", s) < 0 for s in STRATEGIES)


# --- the api binding ---------------------------------------------------------


class TestApiBinding:
    def test_a_retrieval_without_the_strategy_aware_api_is_named(self):
        def old_reindex(db, repo_id, path, sha):  # noqa: ANN001 -- mimics the pre-strategy signature
            return 0

        with pytest.raises(RetrievalUnavailable, match=r"reindex_if_stale has no parameter\(s\) strategy"):
            re_._require_parameters(old_reindex, {"strategy"}, "retrieval.index.reindex_if_stale")

    def test_importing_the_module_does_not_import_the_model_stack(self):
        import subprocess
        import sys

        out = subprocess.run(
            [sys.executable, "-c", "import sys, harness.retrieval_eval; print('torch' in sys.modules, 'sentence_transformers' in sys.modules)"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert out == "False False"


# --- a fake api and a real git fixture ---------------------------------------


class FakeApi:
    """Records every call; `ranked` is what `search` returns for every query."""

    def __init__(self, ranked: list[Span] | None = None, chunk_count: int = 100) -> None:
        self.ranked = ranked if ranked is not None else []
        self.chunk_count = chunk_count
        self.reindexed: list[tuple[str, str]] = []
        self.searched: list[tuple[str, str]] = []
        self.queries: list[tuple[str, str | None]] = []
        self.checked_out: list[str] = []

    def api(self) -> RetrievalApi:
        async def reindex(session, repo_id, path, sha, strategy) -> int:
            self.reindexed.append((sha, strategy))
            # The checkout the indexer is handed must be the commit it is told about.
            self.checked_out.append(git_text(path, "rev-parse", "HEAD"))
            return 10 if len([1 for _, s in self.reindexed if s == strategy]) == 1 else 2

        async def search(session, repo_id, query, limit, query_strategy) -> list[Span]:
            self.searched.append((query.semantic, query_strategy))
            return self.ranked

        def build_query(title: str, body: str | None) -> Query:
            self.queries.append((title, body))
            return Query(semantic=f"{title} {body}", keyword="f1")

        return RetrievalApi(reindex=reindex, search=search, build_query=build_query, count_chunks=lambda path: self.chunk_count)


@pytest.fixture
def instances_world(tmp_path):
    """An upstream with three dated commits, cached, and instances at two of them."""
    upstream_path = tmp_path / "upstream"
    old = make_repo(upstream_path, {"pkg/__init__.py": "", "pkg/mod.py": MOD, "tests/test_mod.py": TEST_MOD},
                    date="2020-01-01T00:00:00Z")
    mid = commit_files(upstream_path, {"pkg/extra.py": "X = 1\n"}, "mid", date="2021-01-01T00:00:00Z")
    new = commit_files(upstream_path, {"pkg/extra.py": "X = 2\n"}, "new", date="2022-01-01T00:00:00Z")
    fixture = Upstream(upstream_path, old, new)
    gold = patch_from(fixture, old, {"pkg/mod.py": MOD.replace("return 1\n", "return 100\n", 1)}, tmp_path / "scratch")
    instances_dir = tmp_path / "instances"
    # Written newest-first: date order, not file or id order, must decide.
    write_instance(instances_dir, "psf__requests-2001", base_commit=new, gold_patch=gold)
    write_instance(instances_dir, "psf__requests-2002", base_commit=old, gold_patch=gold)
    write_instance(instances_dir, "psf__requests-2003", base_commit=mid, gold_patch=gold)
    return fixture, instances_dir, tmp_path / "cache", gold, {"old": old, "mid": mid, "new": new}


async def cache_of(world) -> Path:
    fixture, _, cache_dir, *_ = world
    await CloneCache(cache_dir, url_for=lambda repo: str(fixture.path)).get("psf/requests")
    return cache_dir


@pytest.mark.anyio
class TestInstances:
    async def test_instances_come_in_base_commit_date_order(self, instances_world):
        _, instances_dir, *_ = instances_world
        cache_dir = await cache_of(instances_world)
        specs = list(load_instances(instances_dir).values())

        async with open_git_source(cache_dir, "psf/requests") as source:
            instances, skipped = await load_eval_instances(specs, instances_dir, source)

        assert [i.spec.instance_id for i in instances] == ["psf__requests-2002", "psf__requests-2003", "psf__requests-2001"]
        assert instances[0].commit_time < instances[1].commit_time < instances[2].commit_time
        assert skipped == []
        assert instances[0].gold == GoldTargets(("pkg/mod.py",), (Span("pkg/mod.py", 2, 2),))

    async def test_the_cap_keeps_the_earliest(self, instances_world):
        _, instances_dir, *_ = instances_world
        cache_dir = await cache_of(instances_world)
        async with open_git_source(cache_dir, "psf/requests") as source:
            instances, _ = await load_eval_instances(list(load_instances(instances_dir).values()), instances_dir, source, limit=2)
        assert [i.spec.instance_id for i in instances] == ["psf__requests-2002", "psf__requests-2003"]

    async def test_an_instance_without_a_usable_gold_patch_is_listed_not_scored_as_a_miss(self, instances_world):
        _, instances_dir, *_ = instances_world
        cache_dir = await cache_of(instances_world)
        (instances_dir / "psf__requests-2001.gold.patch").unlink()
        (instances_dir / "psf__requests-2003.gold.patch").write_text("this is not a diff")

        async with open_git_source(cache_dir, "psf/requests") as source:
            instances, skipped = await load_eval_instances(list(load_instances(instances_dir).values()), instances_dir, source)

        assert [i.spec.instance_id for i in instances] == ["psf__requests-2002"]
        assert {s.instance_id: s.reason for s in skipped} == {
            "psf__requests-2001": "no psf__requests-2001.gold.patch beside the instance",
            "psf__requests-2003": "gold patch unreadable: text is not a git diff: it has no 'diff --git' header",
        }

    async def test_a_gold_patch_that_is_a_symlink_is_refused_not_followed(self, instances_world, tmp_path):
        _, instances_dir, *_ = instances_world
        cache_dir = await cache_of(instances_world)
        secret = tmp_path / "outside.patch"
        secret.write_text("diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-x\n+y\n")
        (instances_dir / "psf__requests-2001.gold.patch").unlink()
        (instances_dir / "psf__requests-2001.gold.patch").symlink_to(secret)

        async with open_git_source(cache_dir, "psf/requests") as source:
            _, skipped = await load_eval_instances(list(load_instances(instances_dir).values()), instances_dir, source)

        assert any(s.instance_id == "psf__requests-2001" and "refused" in s.reason for s in skipped)

    async def test_a_missing_cache_is_an_error_that_says_what_to_run_and_does_not_clone(self, tmp_path):
        with pytest.raises(RetrievalEvalError, match="run `repolace-eval select` first"):
            async with open_git_source(tmp_path / "nothing", "psf/requests"):
                pass

    async def test_only_allowlisted_repos_have_a_cache_path(self, tmp_path):
        from harness.select_instances import SelectError

        with pytest.raises(SelectError, match="not an allowlisted"):
            async with open_git_source(tmp_path, "evil/repo"):
                pass

    async def test_checkout_moves_the_working_clone_without_touching_the_cache(self, instances_world):
        _, _, _, _, shas = instances_world
        cache_dir = await cache_of(instances_world)
        cache = cache_dir / "psf__requests"
        before = git_text(cache, "rev-parse", "HEAD")
        async with open_git_source(cache_dir, "psf/requests") as source:
            await source.checkout(shas["old"])
            assert git_text(source.path, "rev-parse", "HEAD") == shas["old"]
            assert (source.path / "pkg" / "mod.py").is_file()
        assert git_text(cache, "rev-parse", "HEAD") == before
        assert git_text(cache, "status", "--porcelain") == ""


@pytest.mark.anyio
class TestPlanning:
    async def plan(self, instances_world, chunk_count: int, cap: int = 100) -> RepoPlan:
        _, instances_dir, *_ = instances_world
        cache_dir = await cache_of(instances_world)
        fake = FakeApi(chunk_count=chunk_count)
        async with open_git_source(cache_dir, "psf/requests") as source:
            instances, _ = await load_eval_instances(list(load_instances(instances_dir).values()), instances_dir, source)
            return await plan_repo(fake.api(), source, "psf/requests", instances, cap)

    async def test_a_repo_within_the_cap_is_planned(self, instances_world):
        plan = await self.plan(instances_world, chunk_count=50)
        assert (plan.instances, plan.first_index_chunks, plan.skip_reason) == (3, 50, None)

    async def test_a_repo_over_the_cap_is_skipped_whole_with_the_reason(self, instances_world):
        plan = await self.plan(instances_world, chunk_count=101)
        assert plan.skip_reason is not None and "101 chunks, over --max-chunks-per-repo 100" in plan.skip_reason
        assert "skipped whole" in plan.skip_reason

    async def test_a_repo_with_no_python_chunks_is_skipped(self, instances_world):
        assert "no Python chunks" in (await self.plan(instances_world, chunk_count=0)).skip_reason

    async def test_a_repo_with_no_instances_is_skipped(self, instances_world):
        fake = FakeApi()
        cache_dir = await cache_of(instances_world)
        async with open_git_source(cache_dir, "psf/requests") as source:
            plan = await plan_repo(fake.api(), source, "psf/requests", [], 100)
        assert plan.skip_reason == "no instance has a usable gold patch"

    async def test_the_plan_counts_chunks_at_the_earliest_base_commit(self, instances_world):
        _, _, _, _, shas = instances_world
        cache_dir = await cache_of(instances_world)
        seen: list[str] = []
        fake = FakeApi()
        api = fake.api()
        api = RetrievalApi(api.reindex, api.search, api.build_query,
                           lambda path: seen.append(git_text(path, "rev-parse", "HEAD")) or 5)
        _, instances_dir, *_ = instances_world
        async with open_git_source(cache_dir, "psf/requests") as source:
            instances, _ = await load_eval_instances(list(load_instances(instances_dir).values()), instances_dir, source)
            await plan_repo(api, source, "psf/requests", instances, 100)
        assert seen == [shas["old"]]

    async def test_plan_only_embeds_and_writes_nothing(self, instances_world):
        _, instances_dir, cache_dir, *_ = instances_world
        await cache_of(instances_world)
        fake = FakeApi()
        run = await run_eval(None, fake.api(), instances_dir=instances_dir, cache_dir=cache_dir, plan_only=True)
        assert [(p.repo, p.instances, p.skip_reason) for p in run.plans] == [("psf/requests", 3, None)]
        assert fake.reindexed == [] and fake.searched == [] and run.results == []

    async def test_a_run_over_the_index_budget_is_refused_before_anything_is_embedded(self, instances_world):
        _, instances_dir, cache_dir, *_ = instances_world
        await cache_of(instances_world)
        fake = FakeApi()
        with pytest.raises(RetrievalEvalError, match="1 repo.* x 4 strategies = 4 full indexes, over --max-index-equivalents 3"):
            await run_eval(None, fake.api(), instances_dir=instances_dir, cache_dir=cache_dir, max_index_equivalents=3)
        assert fake.reindexed == []

    async def test_the_default_budget_is_thirty_five(self):
        assert DEFAULT_MAX_INDEX_EQUIVALENTS == 35

    async def test_an_unknown_strategy_is_refused(self, instances_world):
        _, instances_dir, cache_dir, *_ = instances_world
        with pytest.raises(RetrievalEvalError, match="unknown embedding strategy 'bogus'"):
            await run_eval(None, FakeApi().api(), instances_dir=instances_dir, cache_dir=cache_dir, strategies=["bogus"])


# --- rows --------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
class TestEvalRows:
    async def test_a_row_is_created_with_synthetic_ids_inactive_under_installation_minus_one(self, db_session):
        row = await ensure_eval_repo(db_session, "psf/requests", "head_tail")

        assert row.installation_id == EVAL_INSTALLATION_ID == -1
        assert row.github_repo_id == eval_github_repo_id("psf/requests", "head_tail") < 0
        assert row.is_active is False
        assert row.full_name == "eval/psf/requests@head_tail"
        assert (row.owner, row.name) == ("eval", "psf/requests@head_tail")
        installation = await db_session.get(GithubInstallation, -1)
        assert (installation.account_login, installation.account_id, installation.account_type) == (
            "repolace-eval", -1, "Organization",
        )

    async def test_asking_twice_is_the_same_row(self, db_session):
        first = await ensure_eval_repo(db_session, "psf/requests", "truncate")
        second = await ensure_eval_repo(db_session, "psf/requests", "truncate")
        assert first.id == second.id
        assert await db_session.scalar(select(func.count()).select_from(RegisteredRepo)) == 1

    async def test_each_strategy_and_repo_has_its_own_row_and_one_installation(self, db_session):
        for repo in ("psf/requests", "pallets/flask"):
            for strategy in STRATEGIES:
                await ensure_eval_repo(db_session, repo, strategy)
        assert await db_session.scalar(select(func.count()).select_from(RegisteredRepo)) == 8
        assert await db_session.scalar(select(func.count()).select_from(GithubInstallation)) == 1

    async def test_an_unknown_strategy_is_refused(self, db_session):
        with pytest.raises(RetrievalEvalError, match="unknown embedding strategy"):
            await ensure_eval_repo(db_session, "psf/requests", "bogus")

    async def test_an_eval_row_somebody_activated_is_refused_not_indexed_into(self, db_session):
        row = await ensure_eval_repo(db_session, "psf/requests", "truncate")
        row.is_active = True
        await db_session.commit()
        with pytest.raises(RetrievalEvalError, match="not an inactive eval row"):
            await ensure_eval_repo(db_session, "psf/requests", "truncate")

    async def test_an_eval_row_moved_to_another_installation_is_refused(self, db_session):
        row = await ensure_eval_repo(db_session, "psf/requests", "truncate")
        db_session.add(GithubInstallation(id=4242, account_login="acme", account_id=1, account_type="Organization"))
        await db_session.flush()
        row.installation_id = 4242
        await db_session.commit()
        with pytest.raises(RetrievalEvalError, match="not an inactive eval row"):
            await ensure_eval_repo(db_session, "psf/requests", "truncate")


@pytest.mark.anyio
@pytest.mark.db
class TestApiNeverTouchesEvalRows:
    """`_upsert_repos` and `_deactivate_all_repos` are scoped by installation id; pin that."""

    async def seed(self, db_session):
        eval_rows = [await ensure_eval_repo(db_session, "psf/requests", s) for s in ("truncate", "head_tail")]
        db_session.add(GithubInstallation(id=4242, account_login="acme", account_id=1, account_type="Organization"))
        db_session.add(RegisteredRepo(installation_id=4242, github_repo_id=555, owner="acme", name="real",
                                      full_name="acme/real", default_branch="main", private=False, is_active=False))
        await db_session.commit()
        return [r.id for r in eval_rows]

    async def states(self, db_session, ids):
        rows = (await db_session.execute(select(RegisteredRepo).where(RegisteredRepo.id.in_(ids)))).scalars().all()
        for row in rows:
            await db_session.refresh(row)
        return sorted((r.full_name, r.installation_id, r.is_active) for r in rows)

    async def test_a_sync_that_names_other_repos_leaves_eval_rows_inactive(self, db_session):
        from repolace_api.routes.github import _upsert_repos

        ids = await self.seed(db_session)
        before = await self.states(db_session, ids)

        await _upsert_repos(4242, [Repository(id=555, name="real", full_name="acme/real", owner=RepoOwner(login="acme"),
                                              default_branch="main", private=False)], db_session)
        await db_session.commit()

        assert await self.states(db_session, ids) == before
        # The control: the same call did reactivate the repo it is responsible for.
        real = (await db_session.execute(select(RegisteredRepo).where(RegisteredRepo.github_repo_id == 555))).scalar_one()
        await db_session.refresh(real)
        assert real.is_active is True

    async def test_a_sync_with_an_empty_repo_list_deactivates_the_real_repo_and_still_not_the_eval_rows(self, db_session):
        from repolace_api.routes.github import _upsert_repos

        ids = await self.seed(db_session)
        real = (await db_session.execute(select(RegisteredRepo).where(RegisteredRepo.github_repo_id == 555))).scalar_one()
        real.is_active = True
        await db_session.commit()

        await _upsert_repos(4242, [], db_session)
        await db_session.commit()

        await db_session.refresh(real)
        assert real.is_active is False
        assert all(active is False for *_, active in await self.states(db_session, ids))

    async def test_suspending_the_real_installation_does_not_reach_the_eval_rows(self, db_session):
        from repolace_api.routes.github import _deactivate_all_repos

        ids = await self.seed(db_session)
        await _deactivate_all_repos(4242, db_session)
        await db_session.commit()
        assert [(name, inst) for name, inst, _ in await self.states(db_session, ids)] == [
            ("eval/psf/requests@head_tail", -1), ("eval/psf/requests@truncate", -1),
        ]

    async def test_an_installation_id_never_equals_the_eval_one(self, db_session):
        # GitHub installation ids are positive; the eval installation is -1.
        assert EVAL_INSTALLATION_ID < 0


# --- the run -----------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
class TestRun:
    async def test_every_instance_is_indexed_in_date_order_and_scored_per_query_strategy(self, instances_world, db_session_factory):
        _, instances_dir, cache_dir, _, shas = instances_world
        await cache_of(instances_world)
        fake = FakeApi(ranked=[Span("pkg/zzz.py", 1, 3), Span("pkg/mod.py", 1, 4)])

        run = await run_eval(
            db_session_factory, fake.api(), instances_dir=instances_dir, cache_dir=cache_dir,
            strategies=["truncate", "head_tail"], query_strategies=["truncate", "head_tail"],
        )

        # Per strategy: old, mid, new. The indexer was handed each commit's checkout.
        assert fake.reindexed == [
            (shas["old"], "truncate"), (shas["mid"], "truncate"), (shas["new"], "truncate"),
            (shas["old"], "head_tail"), (shas["mid"], "head_tail"), (shas["new"], "head_tail"),
        ]
        assert fake.checked_out == [sha for sha, _ in fake.reindexed]
        assert len(run.results) == 3 * 2 * 2
        assert {(r.strategy, r.query_strategy) for r in run.results} == {
            ("truncate", "truncate"), ("truncate", "head_tail"), ("head_tail", "truncate"), ("head_tail", "head_tail"),
        }
        # Gold: pkg/mod.py line 2. Ranked: a miss at 1, an overlapping chunk (1-4) at 2.
        first = run.results[0].metrics
        assert first.file_rr == 0.5 and first.chunk_rr == 0.5
        assert first.file_recall[5] == 1.0 and first.chunk_recall[5] == 1.0

    async def test_the_query_is_built_from_the_issue_title_and_the_full_statement(self, instances_world, db_session_factory):
        _, instances_dir, cache_dir, *_ = instances_world
        await cache_of(instances_world)
        fake = FakeApi(ranked=[Span("pkg/mod.py", 1, 4)])

        await run_eval(db_session_factory, fake.api(), instances_dir=instances_dir, cache_dir=cache_dir,
                       strategies=["truncate"], query_strategies=["truncate"], max_instances_per_repo=1)

        assert fake.queries == [(
            "f1 returns the wrong value",
            "f1 returns the wrong value\n\nCalling pkg.mod.f1() gives 1, expected 100.",
        )]

    async def test_a_second_instance_of_a_repo_is_an_incremental_index_not_a_new_one(self, instances_world, db_session_factory):
        _, instances_dir, cache_dir, *_ = instances_world
        await cache_of(instances_world)
        run = await run_eval(db_session_factory, FakeApi(ranked=[Span("pkg/mod.py", 1, 4)]).api(), instances_dir=instances_dir,
                             cache_dir=cache_dir, strategies=["truncate"], query_strategies=["truncate"])
        assert [r.chunks_written for r in run.results] == [10, 2, 2]

    async def test_eval_rows_exist_afterwards_inactive_and_one_per_repo_and_strategy(self, instances_world, db_session_factory, db_session):
        _, instances_dir, cache_dir, *_ = instances_world
        await cache_of(instances_world)
        await run_eval(db_session_factory, FakeApi().api(), instances_dir=instances_dir, cache_dir=cache_dir,
                       strategies=["truncate", "windows"], query_strategies=["truncate"])

        rows = (await db_session.execute(select(RegisteredRepo).order_by(RegisteredRepo.full_name))).scalars().all()
        assert [(r.full_name, r.installation_id, r.is_active) for r in rows] == [
            ("eval/psf/requests@truncate", -1, False), ("eval/psf/requests@windows", -1, False),
        ]

    async def test_a_run_twice_reuses_the_eval_rows(self, instances_world, db_session_factory, db_session):
        _, instances_dir, cache_dir, *_ = instances_world
        await cache_of(instances_world)
        for _ in range(2):
            await run_eval(db_session_factory, FakeApi().api(), instances_dir=instances_dir, cache_dir=cache_dir,
                           strategies=["truncate"], query_strategies=["truncate"])
        assert await db_session.scalar(select(func.count()).select_from(RegisteredRepo)) == 1

    async def test_a_repo_over_the_chunk_cap_is_skipped_and_nothing_of_it_is_indexed(self, instances_world, db_session_factory, db_session):
        _, instances_dir, cache_dir, *_ = instances_world
        await cache_of(instances_world)
        fake = FakeApi(chunk_count=500)
        run = await run_eval(db_session_factory, fake.api(), instances_dir=instances_dir, cache_dir=cache_dir,
                             max_chunks_per_repo=100)
        assert run.results == [] and fake.reindexed == []
        assert "over --max-chunks-per-repo 100" in run.plans[0].skip_reason
        assert await db_session.scalar(select(func.count()).select_from(RegisteredRepo)) == 0

    async def test_an_issue_that_yields_an_empty_query_stops_the_run_loudly(self, instances_world, db_session_factory):
        _, instances_dir, cache_dir, *_ = instances_world
        await cache_of(instances_world)
        fake = FakeApi()
        api = fake.api()
        empty = RetrievalApi(api.reindex, api.search, lambda title, body: Query("  ", ""), api.count_chunks)
        with pytest.raises(RetrievalEvalError, match="empty query"):
            await run_eval(db_session_factory, empty, instances_dir=instances_dir, cache_dir=cache_dir,
                           strategies=["truncate"], query_strategies=["truncate"])


class TestReporting:
    def run_with_results(self) -> EvalRun:
        from harness.metrics import score_query
        from harness.retrieval_eval import InstanceResult

        run = EvalRun(("truncate", "head_tail"), ("truncate",), plans=[RepoPlan("psf/requests", 2, 40), RepoPlan("pallets/flask", 1, 900, "too big")])
        hit = score_query([Span("a.py", 1, 5)], {"a.py"}, [Span("a.py", 2, 2)])
        miss = score_query([Span("b.py", 1, 5)], {"a.py"}, [Span("a.py", 2, 2)])
        nothing = score_query([Span("b.py", 1, 5)], set(), [])
        for strategy, metrics in (("truncate", [hit, miss, nothing]), ("head_tail", [hit, hit, nothing])):
            for i, m in enumerate(metrics):
                run.results.append(InstanceResult("psf/requests", strategy, "truncate", f"id-{i}", m, 7))
        return run

    def test_summary_groups_by_index_and_query_strategy(self):
        summary = summarize(self.run_with_results())
        assert set(summary) == {("truncate", "truncate"), ("head_tail", "truncate")}
        assert summary[("truncate", "truncate")].file_recall[5].mean == 0.5
        assert summary[("head_tail", "truncate")].file_recall[5].mean == 1.0
        assert summary[("truncate", "truncate")].file_recall[5].skipped == 1

    def test_markdown_states_the_plan_the_skip_and_the_unscored_queries(self):
        text = to_markdown(self.run_with_results())
        assert "| pallets/flask | 1 | 900 | too big |" in text
        assert "| truncate / truncate | 2/3 | 50.0% |" in text
        assert "| head_tail / truncate | 2/3 | 100.0% |" in text
        assert "never scored as misses" not in text  # no skipped instances in this run

    def test_json_carries_the_same_numbers(self):
        document = json.loads(to_json(self.run_with_results()))
        assert document["results"][0]["strategy"] == "truncate"
        assert document["plans"][1]["skip_reason"] == "too big"


class TestCommandLine:
    def test_help_exits_zero(self, capsys):
        assert re_.main(["--help"]) == 0
        assert "--max-index-equivalents" in capsys.readouterr().out

    def test_a_bad_flag_is_a_usage_error(self):
        assert re_.main(["--nope"]) == 2

    def test_a_missing_cache_is_a_clean_failure(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(re_, "load_retrieval_api", lambda: FakeApi().api())
        instances = tmp_path / "instances"
        write_instance(instances, "psf__requests-9", base_commit="a" * 40, gold_patch="")
        code = re_.main(["--plan", "--instances-dir", str(instances), "--cache-dir", str(tmp_path / "none")])
        assert code == 1
        assert "run `repolace-eval select` first" in capsys.readouterr().err

    def test_a_retrieval_without_the_strategy_aware_api_is_a_clean_failure(self, tmp_path, capsys, monkeypatch):
        def unavailable():
            raise RetrievalUnavailable("reindex_if_stale has no parameter(s) strategy")

        monkeypatch.setattr(re_, "load_retrieval_api", unavailable)
        assert re_.main(["--plan", "--instances-dir", str(tmp_path), "--cache-dir", str(tmp_path)]) == 1
        assert "no parameter(s) strategy" in capsys.readouterr().err


# --- the real api, with the shared fake embedder -----------------------------


def _real_api_or_skip() -> RetrievalApi:
    try:
        return re_.load_retrieval_api()
    except RetrievalUnavailable as exc:
        pytest.skip(f"needs stream C's strategy-aware retrieval API: {exc}")


@pytest.mark.anyio
@pytest.mark.db
class TestAgainstTheRealRetrieval:
    async def test_two_strategies_index_and_score_with_the_fake_embedder(self, tmp_path, db_session_factory, db_session, monkeypatch):
        api = _real_api_or_skip()
        from retrieval.testing import install_fake_embedder

        embedder = install_fake_embedder(monkeypatch)
        upstream = build_upstream(tmp_path / "up")
        plain = Upstream(upstream.path, upstream.base, upstream.base)
        gold = patch_from(plain, upstream.base, {"pkg/mod.py": MOD.replace("return 1\n", "return 100\n", 1)}, tmp_path / "scratch")
        instances_dir = tmp_path / "instances"
        write_instance(instances_dir, "psf__requests-3001", base_commit=upstream.base, gold_patch=gold)
        cache_dir = tmp_path / "cache"
        await CloneCache(cache_dir, url_for=lambda repo: str(upstream.path)).get("psf/requests")

        run = await run_eval(
            db_session_factory, api, instances_dir=instances_dir, cache_dir=cache_dir,
            strategies=["truncate", "head_tail"], query_strategies=["truncate", "head_tail"],
        )

        assert len(run.results) == 2 * 2
        assert all(r.metrics.file_recall[20] is not None for r in run.results)
        assert {strategy for _, strategy in embedder.text_calls} == {"truncate", "head_tail"}
        assert {strategy for _, strategy in embedder.query_calls} == {"truncate", "head_tail"}
        chunks = await db_session.scalar(select(func.count()).select_from(CodeChunk))
        assert chunks and chunks > 0
        rows = (await db_session.execute(select(RegisteredRepo))).scalars().all()
        assert {r.full_name for r in rows} == {"eval/psf/requests@truncate", "eval/psf/requests@head_tail"}
        assert all(not r.is_active and r.installation_id == -1 for r in rows)
