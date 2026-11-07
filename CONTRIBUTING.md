# Contributing

repolace is a solo project that welcomes issues and small pull requests. Read
[CLAUDE.md](CLAUDE.md) and the matching file in [.claude/rules/](.claude/rules/) before changing an area:
they hold the reasoning behind the decided choices, so please do not re-open one without a new reason.

## Setup

```bash
uv sync --all-packages      # plain `uv sync` prunes the workspace members
```

Python 3.12 or newer. Running tasks and the Docker tests also need the rootless Docker daemon, git 2.38 or
newer and `prlimit`; see [docs/getting-started.md](docs/getting-started.md).

## Checks before a pull request

```bash
uv run ruff check .
uv run --all-packages pytest -m "not db and not docker"       # no services needed
REPOLACE_TEST_DB_SUFFIX=_mybranch uv run --all-packages pytest -m db   # needs Postgres
```

- Database tests truncate every table of their database. Parallel checkouts must set different
  `REPOLACE_TEST_DB_SUFFIX` values (an underscore, then 1 to 32 of `a-z`, `0-9`, `_`).
- After touching models, run `alembic check`. Never run `alembic upgrade` against a development database
  from a test run.
- **Test file names must be unique across the repository** (there are no `__init__.py` files). Prefix new
  files with their area. This must print nothing:
  `find . -name 'test_*.py' -not -path './.venv/*' -not -path './*/.venv/*' | xargs -n1 basename | sort | uniq -d`
- A `# noqa` carries its reason after ` -- `.
- Update the matching rule file and docs in the same change as the code.

## Commits and pull requests

- Please sign your commits. Keep each commit to one logical change.
- Branch from `main`; open the pull request against `main`.
- Never commit secrets. Provider keys live in `.env` and `infra/*.env`, which are gitignored; the
  `*.env.example` files hold names only and a test keeps them honest.

## Reporting security problems

See [SECURITY.md](SECURITY.md).
