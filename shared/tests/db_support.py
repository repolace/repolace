"""Row builders for database-backed tests.

Imported by name rather than injected as fixtures, matching the existing
`<package>_support.py` convention. The names are globally unique because
pytest's prepend import mode puts every test directory on sys.path, so two
files called `support.py` would silently resolve to whichever was collected
first -- the same shadowing that once broke four `app/` packages.
"""

import uuid

from repolace_shared.db.models import GithubInstallation, RegisteredRepo, Task, TaskStatus

INSTALLATION_ID = 4242


def make_installation(**overrides) -> GithubInstallation:
    fields = {
        "id": INSTALLATION_ID,
        "account_login": "acme",
        "account_id": 1,
        "account_type": "Organization",
    }
    return GithubInstallation(**{**fields, **overrides})


def make_repo(**overrides) -> RegisteredRepo:
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
    return RegisteredRepo(**{**fields, **overrides})


def make_task(repo_id: uuid.UUID, **overrides) -> Task:
    fields = {
        "id": uuid.uuid4(),
        "repo_id": repo_id,
        "issue_number": 1,
        "issue_title": "parse_config crashes on an empty config file",
        "issue_url": "https://github.com/acme/sample/issues/1",
        "target_branch": "main",
        "status": TaskStatus.QUEUED,
    }
    return Task(**{**fields, **overrides})


async def seed_task(session, **task_overrides) -> Task:
    """Installation -> repo -> task, committed. Returns the task."""
    session.add(make_installation())
    repo = make_repo()
    session.add(repo)
    await session.flush()
    task = make_task(repo.id, **task_overrides)
    session.add(task)
    await session.commit()
    return task
