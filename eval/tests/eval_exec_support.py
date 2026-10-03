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
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path

import httpx

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
        public: Iterable[str] = (),
        fail_actions: Iterable[str] = (),
        reject_create: Iterable[str] = (),
        fail_delete: Iterable[str] = (),
        echo_credentials: bool = False,
    ) -> None:
        self._ids = itertools.count(1000)
        self.repos: dict[str, dict] = {}
        for name in existing:
            self.repos[name] = {"id": next(self._ids), "private": True, "actions_disabled": False}
        for name in public:
            self.repos[name] = {"id": next(self._ids), "private": False, "actions_disabled": False}
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
        return {"id": repo["id"], "full_name": f"repolace/{name}", "private": repo["private"]}

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
            self.repos[name] = {"id": next(self._ids), "private": body["private"], "actions_disabled": False}
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


# --- fake children --------------------------------------------------------------

#: A child that records its argv and its environment, then exits with a chosen code.
RECORDING_CHILD = """
import json, os, sys, time
out = os.environ["CHILD_RECORD_DIR"]
task = sys.argv[1]
with open(os.path.join(out, task + ".json"), "w") as handle:
    json.dump({"argv": sys.argv[1:], "gateway": os.environ.get("GATEWAY_STAGE_MODELS"),
               "has_bench_token": "REPOLACE_BENCH_GITHUB_TOKEN" in os.environ,
               "start": time.time()}, handle)
time.sleep(float(os.environ.get("CHILD_SLEEP", "0")))
with open(os.path.join(out, task + ".end"), "w") as handle:
    handle.write(str(time.time()))
sys.exit(int(os.environ.get("CHILD_EXIT", "0")))
"""


def child_command(script: str = RECORDING_CHILD) -> tuple[str, ...]:
    import sys

    return (sys.executable, "-c", script)


def task_ids(count: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=index + 1) for index in range(count)]


def names(items: Sequence[object]) -> list[str]:
    return [str(item) for item in items]
