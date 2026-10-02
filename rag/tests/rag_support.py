"""Helpers for indexing tests.

Named `rag_support` rather than `support` for the reason `verify_support`
records: pytest's prepend import mode puts every test directory on `sys.path`,
so a module name here has to be unique across the whole workspace.

Deliberately not importing `shared_support`. It is reachable today by that same
accident of `sys.path`, and relying on it would couple two suites that have no
dependency on each other.
"""

import subprocess
import uuid
from pathlib import Path

from repolace_shared.db.models import GithubInstallation, RegisteredRepo

INSTALLATION_ID = 4242

AUTHOR_ARGS = (
    "-c", "user.name=Test",
    "-c", "user.email=test@example.com",
    "-c", "commit.gpgsign=false",
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *AUTHOR_ARGS, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--initial-branch=main", ".")
    return path


def commit_all(path: Path, message: str) -> str:
    """Stage everything, commit, and return the new commit's sha."""
    git(path, "add", "-A")
    git(path, "commit", "-m", message)
    return git(path, "rev-parse", "HEAD")


async def seed_repo(session, **overrides) -> RegisteredRepo:
    """Installation -> repo, committed.

    Built here and not imported from `shared/tests/db_support.py` for the reason
    the module docstring gives: that module is reachable from this suite only by
    accident of `sys.path`.
    """
    session.add(
        GithubInstallation(id=INSTALLATION_ID, account_login="acme", account_id=1, account_type="Organization")
    )
    fields = {
        "id": uuid.uuid4(),
        "installation_id": INSTALLATION_ID,
        "github_repo_id": 99,
        "owner": "acme",
        "name": "sample",
        "full_name": "acme/sample",
        "default_branch": "main",
        "private": False,
    }
    repo = RegisteredRepo(**{**fields, **overrides})
    session.add(repo)
    await session.commit()
    return repo
