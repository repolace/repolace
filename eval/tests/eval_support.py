"""Builders shared by the harness tests.

Named `eval_support` so the module name is unique across the workspace: pytest's
prepend import mode puts every test directory on `sys.path`, so a `support`
here would be shadowed by (or shadow) another suite's.

Everything is local: fixture repositories are built with real `git` (a mocked git
would assert only that argument lists are assembled, and every bug this harness
can have lives in what git actually does with a patch), HTTP is
`httpx.MockTransport`, and nothing here touches the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from repolace_shared.db.models import (
    GithubInstallation,
    LLMCall,
    RegisteredRepo,
    Task,
    TaskOutcome,
    TaskStatus,
    TaskTestRun,
)

if TYPE_CHECKING:
    from harness.report import TaskRow
    from harness.run_manifest import RunManifest

_GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "HOME": os.environ.get("HOME", ""),
    # No operator config may change what a fixture's git does.
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
}


def git(cwd: Path, *args: str, input_bytes: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, input=input_bytes, env={**_GIT_ENV, **(env or {})},
    )
    return result.stdout


def git_text(cwd: Path, *args: str) -> str:
    return git(cwd, *args).decode("utf-8").strip()


def make_repo(path: Path, files: Mapping[str, str | bytes], *, symlinks: Mapping[str, str] | None = None,
              date: str | None = None) -> str:
    """Create a repository on branch `main` holding `files`, and return its commit sha."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet", "--initial-branch=main")
    return commit_files(path, files, "initial", symlinks=symlinks, date=date)


def commit_files(repo: Path, files: Mapping[str, str | bytes | None], message: str, *,
                 symlinks: Mapping[str, str] | None = None, date: str | None = None) -> str:
    """Write `files` (None deletes), commit everything, return the new sha."""
    for relative, content in files.items():
        target = repo / relative
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    for relative, destination in (symlinks or {}).items():
        link = repo / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(destination)
    git(repo, "add", "--all")
    env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {}
    git(repo, "commit", "--quiet", "--message", message, env=env)
    return git_text(repo, "rev-parse", "HEAD")


def diff_between(repo: Path, old: str, new: str, *, renames: bool = False) -> str:
    """The text of `git diff old new`, as GitHub would serve it (paths quoted by git's defaults)."""
    flag = "--find-renames" if renames else "--no-renames"
    return git(repo, "diff", "--no-color", flag, old, new).decode("utf-8")


# --- a fixture upstream repository -------------------------------------------

MOD = "".join(f"def f{i}():\n    return {i}\n" for i in range(1, 16))
TEST_MOD = "from pkg.mod import f1\n\n\ndef test_it():\n    assert f1() == 1\n"
CRLF_TEST = "def test_a():\r\n    assert True\r\n\r\ndef test_b():\r\n    assert True\r\n"
#: Line 1 is not UTF-8; the rest is ASCII, so a patch to line 20 never mentions it.
LEGACY = b"# caf\xe9 latin-1\n" + b"".join(f"x{i} = {i}\n".encode() for i in range(2, 30))


@dataclass(frozen=True)
class Upstream:
    path: Path
    #: A commit with no symlink; the default branch moves past it.
    base: str
    #: A later commit that adds a symlink (`LICENSE -> README.md`).
    base_symlink: str


def build_upstream(path: Path) -> Upstream:
    """A small upstream repository on `main`: a package, its tests, a CRLF test and a latin-1 file."""
    base = make_repo(path, {
        "pkg/__init__.py": "",
        "pkg/mod.py": MOD,
        "tests/test_mod.py": TEST_MOD,
        "tests/test_crlf.py": CRLF_TEST,
        "legacy.py": LEGACY,
        "README.md": "readme\n",
    })
    base_symlink = commit_files(path, {}, "add a symlink", symlinks={"LICENSE": "README.md"})
    return Upstream(path, base, base_symlink)


def patch_from(upstream: Upstream, base: str, files: Mapping[str, str | bytes | None], scratch: Path) -> str:
    """The diff of `files` applied on top of `base`, without touching `upstream`."""
    scratch.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=scratch)) / "work"
    git(scratch, "clone", "--quiet", str(upstream.path), str(work))
    git(work, "checkout", "--quiet", base)
    new = commit_files(work, files, "change")
    return diff_between(work, base, new)


def write_instance(
    directory: Path,
    instance_id: str,
    *,
    base_commit: str,
    gold_patch: str | None,
    repo: str = "psf/requests",
    problem_statement: str = "f1 returns the wrong value\n\nCalling pkg.mod.f1() gives 1, expected 100.",
) -> None:
    """An instance file, and (unless `gold_patch` is None) its `.gold.patch` sidecar."""
    from repolace_shared.instances import InstanceSpec, dump_instance

    directory.mkdir(parents=True, exist_ok=True)
    spec = InstanceSpec(
        instance_id=instance_id, repo=repo, base_commit=base_commit, version="2.0",
        problem_statement=problem_statement, issue_number=int(instance_id.rsplit("-", 1)[1]),
        fail_to_pass=("tests/test_mod.py::test_it",), pass_to_pass=(),
        test_files={"tests/test_mod.py": TEST_MOD}, gold_files={"pkg/mod.py": MOD},
        spec={"base_image": "python:3.9-slim", "install": ["pip install -e ."]},
    )
    dump_instance(spec, directory / f"{instance_id}.json")
    if gold_patch is not None:
        (directory / f"{instance_id}.gold.patch").write_text(gold_patch, encoding="utf-8")


# --- dataset rows and the datasets-server ------------------------------------


def dataset_row(
    *,
    instance_id: str = "psf__requests-1001",
    repo: str = "psf/requests",
    base_commit: str = "a" * 40,
    patch: str,
    test_patch: str,
    version: str = "2.0",
    fail_to_pass: Sequence[str] = ("tests/test_mod.py::test_it",),
    pass_to_pass: Sequence[str] = ("tests/test_mod.py::test_other",),
    problem_statement: str = "Fix the thing\n\nIt is broken.",
) -> dict[str, Any]:
    """One dataset row shaped as the datasets-server serves it (the id lists are JSON strings)."""
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": base_commit,
        "version": version,
        "problem_statement": problem_statement,
        "patch": patch,
        "test_patch": test_patch,
        "FAIL_TO_PASS": json.dumps(list(fail_to_pass)),
        "PASS_TO_PASS": json.dumps(list(pass_to_pass)),
        "hints_text": "",
        "created_at": "2020-01-01T00:00:00Z",
    }


def dataset_transport(
    rows: Sequence[Mapping[str, Any]],
    *,
    script: Sequence[int | Exception] = (),
    truncate_paged: Sequence[int] = (),
    truncate_always: Sequence[int] = (),
    claimed_total: int | None = None,
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """A fake datasets-server `rows` endpoint.

    `script` is consumed one entry per request before normal service starts: an int
    is an HTTP status to answer with, an exception is raised as a transport error.
    `truncate_paged` rows come back with `truncated_cells` only inside a multi-row
    page (so a one-row re-request returns them whole, as the real server's size cut
    would); `truncate_always` rows are cut however they are asked for.
    """
    pending = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if pending:
            action = pending.pop(0)
            if isinstance(action, Exception):
                raise action
            return httpx.Response(action, text="scripted failure")
        params = request.url.params
        offset, length = int(params["offset"]), int(params["length"])
        page = []
        for index in range(offset, min(offset + length, len(rows))):
            cut = index in truncate_always or (index in truncate_paged and length > 1)
            row = dict(rows[index])
            if cut:
                row["patch"] = row["patch"][:10]
            page.append({"row_idx": index, "row": row, "truncated_cells": ["patch"] if cut else []})
        return httpx.Response(200, json={
            "features": [],
            "rows": page,
            "num_rows_total": claimed_total if claimed_total is not None else len(rows),
            "num_rows_per_page": 100,
            "partial": False,
        })

    return httpx.MockTransport(handler)


def swebench_table(extra: Mapping[str, Any] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """A tiny stand-in for the vendored snapshot. Not SWE-bench's real constants."""
    table: dict[str, dict[str, dict[str, Any]]] = {
        "psf/requests": {
            "2.0": {"python": "3.9", "install": "pip install -e .", "pip_packages": ["pytest==6.2.5"]},
            "0.1": {"python": "3.6", "install": "pip install -e .", "apt_pkgs": ["libffi-dev"]},
            "0.2": {"python": "3.6", "install": "pip install -e ."},
        },
        "pydata/xarray": {"2022.03": {"python": "3.10", "packages": "environment.yml"}},
    }
    table.update(extra or {})
    return table


def write_specs_file(path: Path, table: Mapping[str, Any]) -> Path:
    path.write_text(json.dumps({"schema_version": 1, "specs": table}), encoding="utf-8")
    return path


# --- report rows -------------------------------------------------------------

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def task_row(
    *,
    instance_id: str = "psf__requests-1001",
    run_index: int = 0,
    eval_run_id: str = "run-a",
    status: TaskStatus = TaskStatus.COMPLETED,
    outcome: TaskOutcome | None = TaskOutcome.PASSED,
    cost: str | None = "0.50",
    seconds: float | None = 60.0,
    model: str | None = "anthropic/test-main",
    **overrides: Any,
) -> TaskRow:
    # Imported here so the git/HTTP builders above stay usable on their own.
    from harness.report import TaskRow

    values: dict[str, Any] = dict(
        eval_run_id=eval_run_id,
        instance_id=instance_id,
        run_index=run_index,
        status=status,
        outcome=outcome,
        agent_stop_reason="submitted",
        attempts=1,
        model=model,
        llm_calls=3,
        cost_usd=Decimal(cost) if cost is not None else None,
        input_tokens=1000,
        cached_input_tokens=400,
        output_tokens=100,
        started_at=T0,
        completed_at=T0 + timedelta(seconds=seconds) if seconds is not None else None,
        repo="psf/requests",
        targeted_p2p=False,
    )
    values.update(overrides)
    return TaskRow(**values)


def rows_for(outcomes: Sequence[TaskOutcome | None], *, run_index: int = 0, prefix: str = "inst",
             **overrides: Any) -> list[TaskRow]:
    """One finished COMPLETED row per outcome, instances `inst-0`, `inst-1`, ..."""
    return [
        task_row(instance_id=f"{prefix}-{i}", run_index=run_index, outcome=outcome, **overrides)
        for i, outcome in enumerate(outcomes)
    ]



# --- database seeding --------------------------------------------------------


async def seed_bench_repo(session, *, github_repo_id: int = 7001) -> RegisteredRepo:
    """An installation and one registered repo, committed. The bench repo tasks hang off."""
    if await session.get(GithubInstallation, 4242) is None:
        session.add(GithubInstallation(id=4242, account_login="acme", account_id=1, account_type="Organization"))
    repo = RegisteredRepo(
        installation_id=4242, github_repo_id=github_repo_id, owner="acme", name=f"bench-{github_repo_id}",
        full_name=f"acme/bench-{github_repo_id}", default_branch="main", private=True,
    )
    session.add(repo)
    await session.commit()
    return repo


async def add_eval_task(session, repo: RegisteredRepo, *, instance_id: str = "psf__requests-1001",
                        eval_run_id: str = "run-a", run_index: int = 0, **fields: Any) -> Task:
    values: dict[str, Any] = dict(
        repo_id=repo.id, issue_number=1001, issue_title="Fix the thing",
        issue_url="https://github.com/acme/bench/issues/1001", target_branch=f"bench/{instance_id}",
        status=TaskStatus.COMPLETED, eval_run_id=eval_run_id, instance_id=instance_id, run_index=run_index,
    )
    values.update(fields)
    task = Task(**values)
    session.add(task)
    await session.commit()
    return task


def add_call(session, task: Task, *, model: str = "anthropic/test-main", cost: str | None = "0.10",
             input_tokens: int | None = 100, cached: int | None = 40, output: int | None = 10,
             created_at: datetime | None = None) -> None:
    session.add(LLMCall(
        task_id=task.id, stage="agent", model=model, provider=model.split("/")[0],
        input_tokens=input_tokens, cached_input_tokens=cached, output_tokens=output,
        cost_usd=Decimal(cost) if cost is not None else None, created_at=created_at or T0,
    ))


def add_test_run(session, task: Task, attempt: int, *, error: str | None = None) -> None:
    session.add(TaskTestRun(task_id=task.id, attempt=attempt, commit_sha="c" * 40, error=error))


def run_manifest(
    instance_ids: Sequence[str] | int = 10,
    *,
    runs: int = 3,
    run_id: str = "run-a",
    model: str = "anthropic/test-main",
    agent: str = "llm",
    git_sha: str = "c0ffee" + "0" * 34,
    prefix: str = "inst",
) -> "RunManifest":
    """A manifest for `prefix-0 .. prefix-(n-1)` (or the given ids), `runs` runs each."""
    from harness.run_manifest import RunManifest

    ids = tuple(f"{prefix}-{i}" for i in range(instance_ids)) if isinstance(instance_ids, int) else tuple(instance_ids)
    return RunManifest(
        eval_run_id=run_id, created_at="2026-10-03T12:00:00+00:00", git_sha=git_sha, model=model,
        stage_models={"agent": model}, limits={"max_usd": 2.0}, runs_per_instance=runs,
        instance_ids=ids, agent=agent,
    )
