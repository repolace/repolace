# Cloud work: rules for every package

You are a Claude Code cloud session doing **one** code-editing work package for repolace. Read this file
first, then your `PACKAGE-<X>.md`, then `CLAUDE.md` (a short brief) and the `.claude/rules/*.md` files for
the area you touch (Claude Code loads them when you open a matching file). Do not read `.env`; you have none.

You have nothing to decide except your package. Everything else is settled: **do not relitigate any
"decided" choice in `CLAUDE.md` or the rules.** If a package seems to need a decision that is not written
down, make the smallest choice that keeps the package's acceptance tests honest, record it in your report
under "Choices", and carry on. Ask the user only for a secret, a local run, or a conflict between two
documents; otherwise do not stop to ask.

## Where things run

| Cloud (you) | Local (the user) |
|---|---|
| edit code, run unit and database tests (Postgres + mocks + git in temp directories), `ruff`, `alembic`, `docker compose config` | Docker (the sandbox tests, compose up), the user's database, GitHub App key and tokens, every real model call, the Phase 1 benchmark |

Docker-marked tests (`-m docker`) skip in the cloud. That is expected. Never write a test that needs a
real provider key, a real GitHub token or a Docker daemon without the existing markers/skips.

## Environment bootstrap (Debian or Ubuntu cloud box, no Docker)

These commands are the intended path and **have not been run on a cloud box**. If one fails, fix it, keep
going, and put the working command in your report.

```bash
sudo apt-get update
sudo apt-get install -y postgresql postgresql-contrib postgresql-16-pgvector git util-linux build-essential
git --version            # needs >= 2.38
which prlimit            # util-linux; the agent's grep tool tests need it
pip install uv           # or: curl -LsSf https://astral.sh/uv/install.sh | sh
sudo service postgresql start
sudo -u postgres psql -c "CREATE ROLE repolace SUPERUSER LOGIN PASSWORD 'repolace'"
sudo -u postgres createdb -O repolace repolace
uv sync --all-packages   # plain `uv sync` prunes workspace members
```

Environment for tests (dummy values; the tests never connect to Redis or RabbitMQ):

```bash
export DATABASE_URL=postgresql+asyncpg://repolace:repolace@localhost:5432/repolace
export REDIS_URL=redis://localhost:6379/0
export CELERY_BROKER_URL=amqp://guest:guest@localhost:5672//
export REPOLACE_TEST_DB_REQUIRED=1                 # a missing Postgres must fail, not skip
export REPOLACE_TEST_DB_SUFFIX=_pkg_<x>            # your own test database; never share one
```

The database tests `TRUNCATE` every table of `<database>_test<suffix>`; they never touch the development
database. Do **not** run `alembic upgrade` against `repolace`; the `-m db` fixtures migrate their own database.

## Running checks

Run packages one at a time. A single combined run is too slow.

```bash
uv run ruff check .                     # also: --extend-select RUF100 for stale noqa
for p in shared api gateway agents verify pipeline worker rag; do uv run pytest $p -q -m "not docker"; done
uv run pytest eval/tests -q -m "not docker"          # eval/tests only: eval/cache holds cloned repos
(cd api && uv run alembic heads && uv run alembic check)   # one head; models match migrations
find . -name 'test_*.py' -not -path './.venv/*' -not -path './eval/cache/*' | xargs -n1 basename | sort | uniq -d   # prints nothing
docker compose -f infra/docker-compose.yml config -q       # if you touched infra/ (needs the docker CLI; skip if absent)
```

Expected on `main` today: every package green; the Docker-marked tests skip.

## Definition of done

1. Every behaviour you add or fix has a test that **fails without your change** (prove it by temporarily
   disabling the change, once, and say so in the report).
2. `ruff` (including `RUF100`) clean; test file basenames unique across the repo; one alembic head and
   `alembic check` clean if you touched models or migrations.
3. The rule file in `.claude/rules/` for the area you changed is updated in the same PR. `CLAUDE.md` is
   touched only for a one-line status change if your package says so.
4. A PR from your branch to the target named in your package, with a description: what, why, what you ran,
   what you did **not** run (Docker, real providers) and why. Never write "verified" for something you only read.
5. Commits end with `Co-Authored-By: Claude <noreply@anthropic.com>`; the PR body ends with
   `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

## Hard boundaries

* **Never merge a PR. Never push to `main`.** Push only your own branch.
* Never put a secret, key, token or `.env` content in a file, log, test or PR. Never print the environment.
* Touch **only** the files your package owns. Another package owns the rest: see its "Must not touch" list.
  If you find a bug outside your files, put it in your report under "Found elsewhere"; do not fix it.
* Do not edit the Phase 1 benchmark path (`eval/harness/` scoring and report, `verify/`, `agents/feedback.py`,
  the no-oracle tripwire tests) or the sandbox/Verify security code, except where your package says so.
* Do not weaken, skip or delete an existing test to make yours pass. If an existing test is wrong, say why in
  the report and fix the test in a separate commit with the reason in the message.
* Test filenames must be unique repo-wide (prefix them with your area).

## Plan first, then work

You may be started in plan mode. If so, read first and write a plan that names the files you will change,
the tests you will write (and which existing tests prove nothing regresses), the order of work, and the risks
you see. Then wait to be approved. After approval, follow the same order below. Do not run the bootstrap or
edit anything while planning.

## Working order, and finishing

Work in this order: read, write the failing tests, implement, run the checks, update the rule file, PR, report.
Do not run the whole test suite after every edit; run the package you touched, and the full set once at the end.

Prefer a smaller package that is green and honest over a larger one that is half done. If you cannot finish
the whole package, commit what is green, push the branch, open the PR as a **draft**, and say in the report
exactly what is left. A half-edited tree is never a result.

## Style

* Chat and progress output: use the `caveman` skill (level full). Files, code, comments, docstrings, commit
  messages and the PR body: normal clear prose. Security warnings and the report's "Choices", "Found
  elsewhere" and "Not done" sections stay unambiguous.
* Match the surrounding code: comment density, naming, error handling. Write comments that say why.

## Report (your last message, and the PR body's end)

```
DONE        what now works, one line each, with the test that proves it
RAN         exact commands and their results (counts), and "failed without the change" evidence
NOT RUN     what you could not run here (Docker, providers) and the risk that leaves
CHOICES     every decision you made that was not written down, and why
FOUND ELSEWHERE   bugs or doubts outside your files, with file:line
NOT DONE    anything in the package you did not finish, and why
```
