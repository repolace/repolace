"""Fixtures shared by every test directory.

A root conftest exists for one reason: a session-scoped async fixture (the
database engine) requires a session-scoped `anyio_backend`, and pytest resolves
the *closest* definition. While each test directory kept its own function-scoped
copy, any session-scoped async fixture raised ScopeMismatch. The per-directory
copies are gone; this is the single definition.
"""

import asyncio
import functools
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy.pool import NullPool


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Pin anyio's parametrisation to asyncio; there is no trio in this project.

    Session-scoped so that session-scoped async fixtures can depend on it.
    anyio's plugin lease-counts its runner globally, so this keeps one event
    loop alive for the whole run -- which is what a session-scoped engine needs.
    """
    return "asyncio"


# --- database fixtures -------------------------------------------------------
#
# These exist because the one defect that ever reached a real run -- a
# MissingGreenlet from reading an ORM attribute after `rollback()` expired it --
# was in a database path with no coverage, while every other test avoided the
# database entirely.

REQUIRE_DB_ENV_VAR = "REPOLACE_TEST_DB_REQUIRED"
#: Appended to the development database's name to get the test database's. An
#: environment variable rather than a constant because `db_session` TRUNCATEs
#: every table before every test, so two test runs that share one database
#: corrupt each other -- with failures that look like real bugs, which is the
#: worst way for them to look. Parallel git worktrees each set their own suffix
#: (`REPOLACE_TEST_DB_SUFFIX=_test_<stream>`); the default is unchanged, so a
#: single checkout behaves exactly as it did before.
TEST_DB_SUFFIX = os.environ.get("REPOLACE_TEST_DB_SUFFIX", "_test")


def _skip_or_fail(reason: str, required_env_var: str = REQUIRE_DB_ENV_VAR) -> None:
    """Skip, unless the caller has declared the dependency mandatory.

    A silent skip is how a CI misconfiguration turns into a green build with
    zero database coverage -- which is the exact hole these fixtures were added
    to close. Setting REPOLACE_TEST_DB_REQUIRED=1 makes that impossible.
    """
    if os.environ.get(required_env_var):
        pytest.fail(f"{reason} (and {required_env_var} is set)")
    pytest.skip(reason)


# --- docker marker -----------------------------------------------------------
#
# Same argument as the database fixtures above, one layer up. The `docker`
# marker was declared in pyproject.toml before anything implemented it, so a
# daemon-backed test would have *passed* on a machine with no daemon -- which
# is worse than not having the test, because the containment assertions are
# exactly the ones that must never pass vacuously.

REQUIRE_DOCKER_ENV_VAR = "REPOLACE_TEST_DOCKER_REQUIRED"
_DOCKER_PROBE_TIMEOUT_SECONDS = 20


@functools.lru_cache(maxsize=1)
def _docker_unavailable() -> str | None:
    """Why the daemon cannot be used, or None if it can. Probed once per session."""
    import subprocess

    binary = os.environ.get("REPOLACE_TEST_DOCKER_BINARY", "docker")
    try:
        probe = subprocess.run(
            [binary, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=_DOCKER_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if probe.returncode != 0:
        return probe.stderr.decode("utf-8", errors="replace").strip()[:300]
    return None


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip a `docker`-marked test when no daemon answers.

    In the hook rather than a fixture so the marker itself is what gates the
    test -- a fixture can be forgotten, and a forgotten one here means an
    integration test that quietly never ran.
    """
    if item.get_closest_marker("docker") is None:
        return
    reason = _docker_unavailable()
    if reason:
        _skip_or_fail(f"docker unavailable: {reason}", REQUIRE_DOCKER_ENV_VAR)


def _create_database(admin_dsn: str, name: str) -> None:
    """CREATE DATABASE via asyncpg, in a worker thread.

    In a thread because there is no running event loop there, whatever the
    session-scoped anyio runner is doing. CREATE DATABASE cannot run inside a
    transaction block, and asyncpg autocommits outside an explicit one.
    """
    import asyncpg

    async def go() -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        except asyncpg.DuplicateDatabaseError:
            pass
        finally:
            await conn.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, go()).result()


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A migrated test database, separate from the development one.

    Synchronous on purpose. `api/alembic/env.py` calls `asyncio.run()` at module
    scope, so `command.upgrade` cannot be invoked from inside a running loop.
    """
    from alembic import command
    from alembic.config import Config
    from pydantic import ValidationError
    from sqlalchemy.engine import make_url

    from repolace_shared.config import SharedSettings

    try:
        dev_url = make_url(SharedSettings().database_url)
    except ValidationError:
        _skip_or_fail("no database configuration (DATABASE_URL unset)")

    test_url = dev_url.set(database=f"{dev_url.database}{TEST_DB_SUFFIX}")
    admin_dsn = dev_url.render_as_string(hide_password=False).replace("+asyncpg", "")

    try:
        _create_database(admin_dsn, test_url.database)
    except Exception as exc:  # asyncpg errors, DNS, refused connections
        _skip_or_fail(f"postgres unavailable: {type(exc).__name__}: {exc}")

    rendered = test_url.render_as_string(hide_password=False)
    # The ONLY override that works: api/alembic/env.py unconditionally does
    # `config.set_main_option("sqlalchemy.url", SharedSettings().database_url)`,
    # so a Config-level URL is silently discarded and migrations would run
    # against the development database. SharedSettings is not lru_cached, so it
    # re-reads this. (api/pipeline get_settings() ARE cached -- don't touch them.)
    with mock.patch.dict(os.environ, {"DATABASE_URL": rendered}):
        command.upgrade(Config(str(Path(__file__).parent / "api" / "alembic.ini")), "head")
        yield rendered


@pytest.fixture(scope="session")
async def db_engine(postgres_url: str):
    from sqlalchemy.ext.asyncio import create_async_engine

    # create_async_engine directly rather than db.session.create_engine, which
    # takes no kwargs and so cannot be given a pool class.
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


def _truncate_statement() -> str:
    """Every mapped table, derived rather than hand-listed so a new one cannot leak."""
    from repolace_shared.db.base import Base
    from repolace_shared.db import models  # noqa: F401  registers the tables

    names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    return f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"


@pytest.fixture
async def db_session(db_engine):
    """A session against a freshly emptied database.

    TRUNCATE rather than a wrapping transaction, because the code under test
    commits: `reindex_if_stale` owns its transaction (its advisory lock is
    transaction-scoped), `run_task` opens two concurrent sessions, and `_fail`
    rolls back before it writes. A SAVEPOINT-based fixture cannot represent any
    of that.

    Truncating *before* rather than after means a crashed test still leaves the
    next one a clean slate, and leaves its own rows behind to be inspected.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with db_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(_truncate_statement()))

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def db_session_factory(db_engine, db_session):
    """A factory for code that opens its own sessions, sharing the truncated database.

    `run_task` takes a factory and opens two sessions from it, so a fixture that
    only handed out one session could not exercise it.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(db_engine, expire_on_commit=False)
