"""Builders for the harness execution-side tests (fork, enqueue, run, gold).

Plain constructors and small fakes rather than fixtures, imported by name. Nothing
here touches the network, GitHub, Docker or a provider: the GitHub side is an
`httpx.MockTransport`, git runs against local repositories only, and "the child
process" is `sys.executable -c ...`.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import httpx

from harness.bench_repos import BENCH_MARKER
from repolace_shared.instances import InstanceSpec, dump_instance

#: Deliberately shaped like nothing `redact` knows (`ghp_...`, `github_pat_...`, a
#: JWT), so a test that finds it scrubbed has proved the *literal* replacement
#: works, not that a pattern happened to match.
TOKEN = "benchtoken-SENTINEL-0123456789abcdefghij"

#: A fixed upstream commit sha for builders that never need a real repository.
BASE_COMMIT = "091991be0da19de9108dbe5e3752917fea3d7fdc"


def make_instance(instance_id: str = "psf__requests-2317", **overrides) -> InstanceSpec:
    fields = {
        "instance_id": instance_id,
        "repo": "psf/requests",
        "base_commit": BASE_COMMIT,
        "version": "2.4",
        "problem_statement": "Requests breaks on bytes methods\n\nLong description with details.",
        "issue_number": 2317,
        "fail_to_pass": ("test_requests.py::RequestsTestCase::test_bytes_method",),
        "pass_to_pass": ("test_requests.py::RequestsTestCase::test_no_content_length",),
        "test_files": {"test_requests.py": "def test_it():\n    assert True\n"},
        "gold_files": {"requests/sessions.py": "# the fix\n"},
        "spec": {"base_image": "python:3.9-slim", "install": ["pip install -e ."]},
    }
    return InstanceSpec(**{**fields, **overrides})


def write_instances(directory: Path, *specs: InstanceSpec) -> None:
    for spec in specs:
        dump_instance(spec, directory / f"{spec.instance_id}.json")


# --- a fake GitHub --------------------------------------------------------------


class FakeGithub:
    """Just enough of the REST API: an organisation's repositories, in memory.

    `events` is shared with `RecordingPush` so a test can assert the order of API
    calls and pushes in one list. `unexpected` collects requests the fake does not
    model; a test that sends one has found a call the module should not make.
    """

    def __init__(
        self,
        *,
        existing: Iterable[str] = (),
        foreign: Iterable[str] = (),
        public: Iterable[str] = (),
        fail_actions: Iterable[str] = (),
        reject_create: Iterable[str] = (),
        fail_delete: Iterable[str] = (),
        echo_credentials: bool = False,
    ) -> None:
        self._ids = itertools.count(1000)
        self.repos: dict[str, dict] = {}
        # `existing`: created by this tool (marker, private). `foreign`: private but made
        # by someone else (no marker). `public`: carries the marker but is public.
        for name in existing:
            self.repos[name] = {"id": next(self._ids), "private": True, "actions_disabled": False, "description": BENCH_MARKER}
        for name in foreign:
            self.repos[name] = {"id": next(self._ids), "private": True, "actions_disabled": False, "description": "somebody else's"}
        for name in public:
            self.repos[name] = {"id": next(self._ids), "private": False, "actions_disabled": False, "description": BENCH_MARKER}
        self.fail_actions = set(fail_actions)
        self.reject_create = set(reject_create)
        self.fail_delete = set(fail_delete)
        self.echo_credentials = echo_credentials
        self.events: list[tuple] = []
        self.requests: list[httpx.Request] = []
        self.bodies: list[object] = []
        self.unexpected: list[tuple[str, str]] = []
        self.installation_adds: list[tuple[int, int]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _repo_json(self, name: str) -> dict:
        repo = self.repos[name]
        return {
            "id": repo["id"], "full_name": f"repolace/{name}", "private": repo["private"],
            "description": repo["description"],
        }

    def _error(self, request: httpx.Request, status: int, message: str) -> httpx.Response:
        if self.echo_credentials:
            # A hostile or merely chatty server that repeats the credential back.
            message = f"{message} (you sent {request.headers.get('authorization')})"
        return httpx.Response(status, json={"message": message})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.bodies.append(body)
        self.events.append((method, path))

        if method == "POST" and path == "/orgs/repolace/repos":
            name = body["name"]
            if name in self.reject_create:
                return httpx.Response(422, json={"message": "Validation Failed", "errors": [{"message": "name is invalid"}]})
            if name in self.repos:
                return httpx.Response(
                    422,
                    json={
                        "message": "Repository creation failed.",
                        "errors": [{"resource": "Repository", "field": "name", "message": "name already exists on this account"}],
                    },
                )
            self.repos[name] = {
                "id": next(self._ids), "private": body["private"], "actions_disabled": False,
                "description": body.get("description"),
            }
            return httpx.Response(201, json=self._repo_json(name))

        match = re.fullmatch(r"/repos/repolace/([^/]+)", path)
        if match and method == "GET":
            name = match.group(1)
            if name not in self.repos:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=self._repo_json(name))
        if match and method == "DELETE":
            name = match.group(1)
            if name in self.fail_delete:
                return self._error(request, 403, "Must have admin rights to Repository.")
            if name not in self.repos:
                return httpx.Response(404, json={"message": "Not Found"})
            del self.repos[name]
            return httpx.Response(204)

        match = re.fullmatch(r"/repos/repolace/([^/]+)/actions/permissions", path)
        if match and method == "PUT":
            name = match.group(1)
            if name in self.fail_actions:
                return self._error(request, 403, "Resource not accessible by personal access token")
            self.repos[name]["actions_disabled"] = body == {"enabled": False}
            return httpx.Response(204)

        match = re.fullmatch(r"/user/installations/(\d+)/repositories/(\d+)", path)
        if match and method == "PUT":
            self.installation_adds.append((int(match.group(1)), int(match.group(2))))
            return httpx.Response(204)

        self.unexpected.append((method, path))
        return httpx.Response(404, json={"message": "Not Found"})


class RecordingPush:
    """A `git_push` seam: records each push, optionally failing chosen repositories."""

    def __init__(self, events: list[tuple] | None = None, *, fail_for: Iterable[str] = (), error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, Path, str]] = []
        self._events = events
        self._fail_for = tuple(fail_for)
        self._error = error

    async def __call__(self, url: str, refspec: str, checkout: Path, token: str) -> None:
        self.calls.append((url, refspec, checkout, token))
        if self._events is not None:
            self._events.append(("PUSH", url, refspec))
        if self._error is not None and any(name in url for name in self._fail_for):
            raise self._error


@contextlib.asynccontextmanager
async def fake_checkout(spec: InstanceSpec, cache_dir: Path):
    """A `checkout` seam that never clones: the tests must not touch the network."""
    yield cache_dir / "fake-checkout"


# --- local git ------------------------------------------------------------------


def run_git_sync(*args: str, cwd: Path) -> str:
    """git for test setup only: pinned config, no signing, no global state."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, env=env, check=True, capture_output=True, text=True,
    )
    return done.stdout.strip()


def make_cached_upstream(cache_dir: Path, repo: str = "psf/requests") -> str:
    """A real one-commit repository where `select` would have cached `repo`; returns its commit sha."""
    owner, name = repo.split("/")
    path = cache_dir / f"{owner}__{name}"
    path.mkdir(parents=True)
    run_git_sync("init", "-q", "-b", "main", cwd=path)
    (path / "README").write_text("upstream\n")
    run_git_sync("add", "README", cwd=path)
    run_git_sync("commit", "-q", "-m", "base", cwd=path)
    return run_git_sync("rev-parse", "HEAD", cwd=path)


# --- database rows ---------------------------------------------------------------

_repo_ids = itertools.count(5000)


async def add_installation(session, installation_id: int = 1) -> None:
    from repolace_shared.db.models import GithubInstallation

    if await session.get(GithubInstallation, installation_id) is None:
        session.add(GithubInstallation(id=installation_id, account_login="repolace", account_id=1, account_type="Organization"))
        await session.commit()


async def add_repo(session, full_name: str, *, is_active: bool = True, installation_id: int = 1):
    """A `registered_repos` row, as the App's installation sync would have written it."""
    from repolace_shared.db.models import RegisteredRepo

    await add_installation(session, installation_id)
    owner, name = full_name.split("/")
    repo = RegisteredRepo(
        installation_id=installation_id, github_repo_id=next(_repo_ids), owner=owner, name=name,
        full_name=full_name, default_branch="main", private=True, is_active=is_active,
    )
    session.add(repo)
    await session.commit()
    return repo


async def add_task(
    session, repo, *, eval_run_id: str | None = "run-1", instance_id: str | None = "a", run_index: int | None = 0,
    status=None, started_at=None, **extra,
):
    from repolace_shared.db.models import Task, TaskStatus

    task = Task(
        repo_id=repo.id, issue_number=1, issue_title="t", issue_url="https://github.com/repolace/x",
        target_branch="main", eval_run_id=eval_run_id, instance_id=instance_id, run_index=run_index,
        status=status or TaskStatus.QUEUED, started_at=started_at, **extra,
    )
    session.add(task)
    await session.commit()
    return task


async def add_llm_call(session, task, cost: str | None) -> None:
    from decimal import Decimal

    from repolace_shared.db.models import LLMCall

    session.add(LLMCall(
        task_id=task.id, stage="agent", model="m", provider="p",
        cost_usd=None if cost is None else Decimal(cost),
    ))
    await session.commit()


# --- fake children --------------------------------------------------------------

#: A stand-in for `repolace-run-task`. It records what it was given, optionally
#: claims its row exactly as the pipeline does (`UPDATE ... WHERE status = 'queued'`),
#: and then does what its per-task plan says: sleep, spend, finish the row, hang,
#: leave a grandchild behind, exit with a chosen code. `REPOLACE_CHILD_PLAN` maps task id (or
#: "*") to the plan; a plan without "claim" never touches the database. Its own settings use
#: the REPOLACE_ prefix because the runner passes the child an allowlisted environment.
RECORDING_CHILD = r"""
import asyncio, json, os, subprocess, sys, time

out = os.environ["REPOLACE_CHILD_RECORD_DIR"]
task = sys.argv[1]
plans = json.loads(os.environ.get("REPOLACE_CHILD_PLAN", "{}"))
plan = plans.get(task, plans.get("*", {}))
dsn = os.environ.get("REPOLACE_CHILD_DB_DSN")


def sql(statement):
    import asyncpg

    async def go():
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(statement, task)
        finally:
            await connection.close()

    asyncio.run(go())


with open(os.path.join(out, task + ".json"), "w") as handle:
    json.dump({"argv": sys.argv[1:], "gateway": os.environ.get("GATEWAY_STAGE_MODELS"),
               "has_bench_token": "REPOLACE_BENCH_GITHUB_TOKEN" in os.environ,
               "has_other": os.environ.get("REPOLACE_CHILD_KEEP_ME"), "env_names": sorted(os.environ),
               "start": time.time(), "pid": os.getpid()}, handle)
print("child output on stdout", flush=True)
print("child output on stderr", file=sys.stderr, flush=True)
if plan.get("claim"):
    sql("UPDATE tasks SET status = 'running', started_at = now() WHERE id = $1::uuid AND status = 'queued'")
if plan.get("grandchild"):
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
    with open(os.path.join(out, task + ".grandchild"), "w") as handle:
        handle.write(str(grandchild.pid))
if plan.get("cost"):
    sql("INSERT INTO llm_calls (id, task_id, stage, model, provider, cost_usd) "
        "VALUES (gen_random_uuid(), $1::uuid, 'agent', 'm', 'p', " + str(plan["cost"]) + ")")
time.sleep(plan.get("sleep", 0))
if plan.get("finish"):
    sql("UPDATE tasks SET status = '" + plan["finish"] + "' WHERE id = $1::uuid")
if plan.get("hang"):
    time.sleep(3600)
with open(os.path.join(out, task + ".end"), "w") as handle:
    handle.write(str(time.time()))
sys.exit(plan.get("exit", 0))
"""


def child_command(script: str = RECORDING_CHILD) -> tuple[str, ...]:
    return (sys.executable, "-c", script)
