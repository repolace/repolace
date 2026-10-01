"""Migrations run in both directions, and the models agree with where they end.

Two things the schema tests above cannot see. They run against a database that
was only ever upgraded, so a `downgrade()` that fails -- or leaves something
behind that the next `upgrade()` then collides with -- is invisible until the
day someone has to roll back. And `alembic check` was a manual step that caught
a real defect once (migration 0004's indexes were missing from the model, so the
next autogenerate would have dropped both retrieval arms); nothing ran it.

Synchronous on purpose: `api/alembic/env.py` calls `asyncio.run()` at module
scope, so `command.upgrade` cannot be invoked from inside a running loop.
"""

import asyncio
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.db

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "api" / "alembic.ini"

#: Everything migration 0013 adds, so "gone" and "present" are one assertion.
TASK_COLUMNS_0013 = {"patch_diff", "score_reason", "agent_stop_reason"}
REPO_COLUMNS_0013 = {"index_strategy"}
INDEX_0013 = "uq_tasks_eval_instance_run"
CHECKS_0013 = {"ck_tasks_agent_stop_reason", "ck_tasks_eval_columns_together"}


def _in_thread(coro_factory):
    """Run a coroutine to completion off the calling thread.

    There is no loop to join here -- the session-scoped anyio runner owns one
    elsewhere -- and `asyncio.run` refuses to start inside a thread that already
    has one running.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro_factory()).result()


def _dsn(url) -> str:
    return url.render_as_string(hide_password=False).replace("+asyncpg", "")


async def _execute(dsn: str, *statements: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


async def _fetch(dsn: str, query: str, *args) -> list[tuple]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        return [tuple(row) for row in await conn.fetch(query, *args)]
    finally:
        await conn.close()


@pytest.fixture
def scratch_url(postgres_url: str) -> Iterator[str]:
    """A throwaway database, dropped and recreated per test, never the shared one.

    Named off the migrated test database so parallel worktrees -- each with its
    own `REPOLACE_TEST_DB_SUFFIX` -- get distinct scratch databases and cannot
    drop each other's. `postgres_url` is requested for its skip-or-fail
    behaviour as much as for the name: no database configured means no test.
    """
    base = make_url(postgres_url)
    scratch = base.set(database=f"{base.database}_migrations")
    # Connect through the shared test database: any database on the server will
    # do for CREATE/DROP of another one.
    admin = _dsn(base)

    _in_thread(
        lambda: _execute(
            admin,
            f'DROP DATABASE IF EXISTS "{scratch.database}"',
            f'CREATE DATABASE "{scratch.database}"',
        )
    )
    try:
        yield scratch.render_as_string(hide_password=False)
    finally:
        _in_thread(lambda: _execute(admin, f'DROP DATABASE IF EXISTS "{scratch.database}"'))


def _config() -> Config:
    return Config(str(ALEMBIC_INI))


def _schema_state(url: str) -> dict[str, set[str]]:
    """The slice of the catalog migration 0013 owns, as plain sets."""
    dsn = _dsn(make_url(url))

    def columns(table: str) -> set[str]:
        rows = _in_thread(
            lambda: _fetch(
                dsn,
                "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
                table,
            )
        )
        return {row[0] for row in rows}

    indexes = _in_thread(
        lambda: _fetch(dsn, "SELECT indexname FROM pg_indexes WHERE indexname = $1", INDEX_0013)
    )
    checks = _in_thread(
        lambda: _fetch(
            dsn,
            "SELECT conname::text FROM pg_constraint WHERE conname::text = ANY($1::text[])",
            sorted(CHECKS_0013),
        )
    )
    return {
        "tasks": columns("tasks") & TASK_COLUMNS_0013,
        "registered_repos": columns("registered_repos") & REPO_COLUMNS_0013,
        "index": {row[0] for row in indexes},
        "check": {row[0] for row in checks},
    }


ABSENT = {"tasks": set(), "registered_repos": set(), "index": set(), "check": set()}
PRESENT = {
    "tasks": TASK_COLUMNS_0013,
    "registered_repos": REPO_COLUMNS_0013,
    "index": {INDEX_0013},
    "check": CHECKS_0013,
}


class TestMigration0013:
    def test_it_upgrades_downgrades_and_upgrades_again(self, scratch_url):
        """Reversible, and re-appliable: the second upgrade is what proves the
        downgrade left nothing behind for it to collide with (an index or a
        constraint of the same name would fail here)."""
        with mock.patch.dict(os.environ, {"DATABASE_URL": scratch_url}):
            command.upgrade(_config(), "0012")
            assert _schema_state(scratch_url) == ABSENT

            command.upgrade(_config(), "0013")
            assert _schema_state(scratch_url) == PRESENT

            command.downgrade(_config(), "0012")
            assert _schema_state(scratch_url) == ABSENT

            command.upgrade(_config(), "0013")
            assert _schema_state(scratch_url) == PRESENT

    def test_the_downgrade_keeps_the_rows_it_did_not_add(self, scratch_url):
        """Additive both ways: rolling back loses the new columns and nothing else."""
        dsn = _dsn(make_url(scratch_url))
        with mock.patch.dict(os.environ, {"DATABASE_URL": scratch_url}):
            command.upgrade(_config(), "0013")
            _in_thread(
                lambda: _execute(
                    dsn,
                    "INSERT INTO github_installations (id, account_login, account_id, account_type) "
                    "VALUES (1, 'acme', 1, 'Organization')",
                    "INSERT INTO registered_repos (id, installation_id, github_repo_id, owner, name, "
                    "full_name, default_branch, private, index_strategy) VALUES "
                    "(gen_random_uuid(), 1, 7, 'acme', 'sample', 'acme/sample', 'main', false, 'head_tail')",
                )
            )

            command.downgrade(_config(), "0012")

            rows = _in_thread(lambda: _fetch(dsn, "SELECT full_name FROM registered_repos"))
            assert rows == [("acme/sample",)]


class TestModelsAgreeWithMigrations:
    def test_alembic_check_is_clean_at_head(self, scratch_url):
        """The model and the migration chain describe the same schema.

        Run against a database built by the migrations alone, so a column, index
        or constraint declared on one side only shows up as a pending operation
        -- which `check` turns into an error. This is the guard that would have
        caught migration 0004's undeclared indexes before they became a
        `drop_index` in somebody's next autogenerate.
        """
        with mock.patch.dict(os.environ, {"DATABASE_URL": scratch_url}):
            command.upgrade(_config(), "head")
            command.check(_config())
