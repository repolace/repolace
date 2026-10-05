"""`repolace-eval fork`: the guardrails that stand in for a dedicated GitHub organisation.

Benchmark repositories live in the organisation that also hosts `repolace/repolace`,
so the only thing standing between this script and the product repository is the
name rule. The first tests try to defeat it; the token tests assert a credential
reaches GitHub in exactly one header and nowhere a person could read it.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

import httpx
import pytest

from eval_exec_support import (
    BASE_COMMIT,
    TOKEN,
    FakeGithub,
    RecordingPush,
    fake_checkout,
    make_cached_upstream,
    make_instance,
    run_git_sync,
    write_instances,
)
from harness import bench_repos
from harness.bench_repos import (
    BENCH_MARKER,
    BenchRepo,
    BenchRepoError,
    GithubBench,
    NotCreatedByThisTool,
    TOKEN_ENV_VAR,
    check_bench_name,
    check_push_target,
    load_bench_entries,
    load_bench_repos,
    main,
    upstream_checkout,
)
from repolace_shared.git.repo import GitCommandError, run_git as shared_run_git

API = "https://api.test"
GIT = "https://git.test"

HOSTILE_NAMES = [
    "repolace",
    "repolace/repolace",
    "bench-x/../repolace",
    "bench-x/..",
    "../repolace",
    "bench-x%2F..%2Frepolace",
    "BENCH-x",
    "Bench-x",
    "bench-x ",
    " bench-x",
    "bench-x\n",
    "bench-x\t",
    "bench-x\x00",
    "bench-x\\y",
    "bench-x/y",
    "bench-",
    "bench",
    "bench_x",
    "bench-é",
    "bench-ｘ",  # fullwidth x: not ASCII
    "",
    ".",
    "..",
    None,
    5,
    ("bench-x",),
    # Legal under the old looser pattern; refused now that the guard is as strict as
    # the instance-id rule, so what is checked is what is sent.
    "bench-x.git",
    "bench-x.GIT",
    "bench-x.Git",
    "bench-.git",
    "bench-.",
    "bench-..",
    "bench-x.",
    "bench-a..b",
    "bench-.x",
    "bench--",
    "bench-_x",
    "bench-" + "a" * 98,
]


@pytest.fixture(autouse=True)
def no_network_git(monkeypatch):
    """Fail any git command aimed at a network URL, whatever a test forgot to fake.

    A real `git clone https://github.com/...` once slipped through a test that
    meant to fake the checkout; this makes that an assertion, not a download.
    """
    real = bench_repos.run_git

    async def guarded(*args, **kwargs):
        if any(isinstance(arg, str) and re.match(r"(https?|git|ssh)://", arg) for arg in args):
            raise AssertionError(f"a test tried to run git against the network: {args[:3]}")
        return await real(*args, **kwargs)

    monkeypatch.setattr(bench_repos, "run_git", guarded)


@pytest.fixture
def workdir(tmp_path: Path) -> dict[str, Path]:
    instances = tmp_path / "instances"
    instances.mkdir()
    return {"instances": instances, "cache": tmp_path / "cache", "mapping": instances / "bench_repos.toml", "root": tmp_path}


def run_fork(
    workdir, github: FakeGithub, push: RecordingPush | None, *extra: str, token: str | None = TOKEN, checkout=fake_checkout
) -> int:
    return main(
        ["--instances-dir", str(workdir["instances"]), "--cache-dir", str(workdir["cache"]), *extra],
        transport=github.transport(),
        token_reader=lambda: token,
        git_push=push,
        checkout=checkout,
        api_url=API,
        git_url=GIT,
    )


def seed(workdir, *instance_ids: str) -> None:
    write_instances(workdir["instances"], *(make_instance(i, issue_number=index + 1) for index, i in enumerate(instance_ids)))


class TestNameGuard:
    @pytest.mark.parametrize("name", HOSTILE_NAMES, ids=repr)
    def test_check_refuses_a_hostile_name(self, name):
        with pytest.raises(BenchRepoError):
            check_bench_name(name)

    @pytest.mark.parametrize("name", HOSTILE_NAMES, ids=repr)
    def test_a_bench_repo_cannot_be_built_from_a_hostile_name(self, name):
        with pytest.raises(BenchRepoError):
            BenchRepo(name)

    @pytest.mark.parametrize(
        "name", ["bench-psf__requests-2317", "bench-a.b_c-d", "bench-1", "bench-a-", "bench-gitx", "bench-a.gitx", "bench-" + "a" * 91]
    )
    def test_a_bench_name_is_accepted(self, name):
        assert BenchRepo(name).full_name == f"repolace/{name}"

    @pytest.mark.parametrize(
        "full_name",
        ["repolace/repolace", "other/bench-x", "bench-x", "repolace/bench-x/../repolace", "repolace/bench-x/", "REPOLACE/bench-x", "", None],
    )
    def test_a_full_name_outside_the_bench_namespace_is_refused(self, full_name):
        with pytest.raises(BenchRepoError):
            BenchRepo.from_full_name(full_name)

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("DELETE", "/repos/repolace/repolace"),
            ("GET", "/repos/repolace/repolace"),
            ("PUT", "/repos/repolace/repolace/actions/permissions"),
            ("PUT", "/repos/repolace/bench-x/../repolace/actions/permissions"),
            ("DELETE", "/repos/repolace/bench-x/../repolace"),
            ("DELETE", "/repos/repolace/bench-x/"),
            ("DELETE", "/repos/repolace/bench-x/actions/permissions"),
            ("DELETE", "/repos/other/bench-x"),
            ("GET", "/repos/other/bench-x"),
            ("POST", "/orgs/other/repos"),
            ("POST", "/repos/repolace/bench-x"),
            ("PATCH", "/repos/repolace/bench-x"),
            ("PUT", "/repos/repolace/bench-x/branches/main/protection"),
            ("PUT", "/user/installations/abc/repositories/1"),
            ("PUT", "/user/installations/1/repositories/1/../2"),
            ("GET", "/user"),
            ("GET", "/repos/repolace/bench-x.git"),
            ("DELETE", "/repos/repolace/bench-x."),
            ("DELETE", "/repos/repolace/bench-a..b"),
            ("PUT", "/repos/repolace/bench-x.git/actions/permissions"),
        ],
    )
    def test_the_request_chokepoint_refuses_an_endpoint_outside_the_allow_list(self, method, path):
        github = FakeGithub()

        async def attempt():
            client = GithubBench(TOKEN, base_url=API, transport=github.transport())
            try:
                await client._request(method, path)
            finally:
                await client.aclose()

        with pytest.raises(BenchRepoError):
            asyncio.run(attempt())
        assert github.requests == []

    def test_a_trailing_newline_does_not_slip_past_the_pattern(self):
        # `re.match(r"...$")` accepts "bench-x\n"; the guard uses fullmatch.
        with pytest.raises(BenchRepoError):
            check_bench_name("bench-x\n")

    def test_the_mapping_file_cannot_name_the_product_repository(self, workdir):
        workdir["mapping"].write_text('"psf__requests-2317" = "repolace/repolace"\n')
        with pytest.raises(BenchRepoError):
            load_bench_repos(workdir["mapping"])

    def test_the_mapping_file_entry_must_be_the_name_derived_from_its_instance_id(self, workdir):
        workdir["mapping"].write_text('"a" = "repolace/bench-b"\n')
        with pytest.raises(BenchRepoError, match="expected"):
            load_bench_repos(workdir["mapping"])

    def test_the_mapping_file_refuses_a_non_string_entry(self, workdir):
        workdir["mapping"].write_text('[a]\nx = "repolace/bench-a"\n')
        with pytest.raises(BenchRepoError):
            load_bench_repos(workdir["mapping"])

    def test_delete_refuses_a_poisoned_mapping_without_sending_anything(self, workdir):
        workdir["mapping"].write_text('"a" = "repolace/repolace"\n')
        github = FakeGithub(existing=["repolace", "bench-a"])
        code = main(
            ["--instances-dir", str(workdir["instances"]), "--delete", "--yes"],
            transport=github.transport(), token_reader=lambda: TOKEN, api_url=API, git_url=GIT,
        )
        assert code == 2
        assert github.requests == []
        assert "repolace" in github.repos


class TestFork:
    def test_creates_a_private_repository_with_issues_and_wiki_off(self, workdir):
        seed(workdir, "psf__requests-2317")
        github = FakeGithub()

        assert run_fork(workdir, github, RecordingPush(github.events)) == 0

        create = github.requests[0]
        assert (create.method, create.url.path) == ("POST", "/orgs/repolace/repos")
        body = github.bodies[0]
        assert body["name"] == "bench-psf__requests-2317"
        assert body["private"] is True
        assert body["has_issues"] is False
        assert body["has_wiki"] is False
        assert body["auto_init"] is False

    def test_actions_are_disabled_before_anything_is_pushed(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()
        push = RecordingPush(github.events)

        assert run_fork(workdir, github, push) == 0

        assert github.events == [
            ("POST", "/orgs/repolace/repos"),
            ("PUT", "/repos/repolace/bench-a/actions/permissions"),
            ("PUSH", f"{GIT}/repolace/bench-a.git", f"{BASE_COMMIT}:refs/heads/main"),
        ]
        assert github.bodies[1] == {"enabled": False}
        assert github.repos["bench-a"]["actions_disabled"] is True
        assert github.unexpected == []

    def test_pushes_the_base_commit_to_main_without_force(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()
        push = RecordingPush(github.events)
        sha = make_cached_upstream(workdir["cache"])
        write_instances(workdir["instances"], make_instance("a", base_commit=sha))

        assert run_fork(workdir, github, push, checkout=upstream_checkout) == 0

        (url, refspec, checkout, _token), = push.calls
        assert url == f"{GIT}/repolace/bench-a.git"
        assert refspec == f"{sha}:refs/heads/main"
        assert "force" not in refspec and not refspec.startswith("+")
        assert checkout == (workdir["cache"] / "psf__requests").resolve()

    def test_writes_the_mapping_for_a_ready_repository(self, workdir):
        seed(workdir, "a", "b")
        github = FakeGithub()

        assert run_fork(workdir, github, RecordingPush(github.events)) == 0

        assert load_bench_repos(workdir["mapping"]) == {"a": "repolace/bench-a", "b": "repolace/bench-b"}

    def test_an_already_existing_repository_is_a_skip_that_is_still_locked_down_and_pushed(self, workdir, capsys):
        seed(workdir, "a")
        github = FakeGithub(existing=["bench-a"])
        push = RecordingPush(github.events)

        assert run_fork(workdir, github, push) == 0

        assert [call[1] for call in push.calls] == [f"{BASE_COMMIT}:refs/heads/main"]
        assert github.repos["bench-a"]["actions_disabled"] is True
        assert "1 ready (0 created, 1 already existed)" in capsys.readouterr().out
        assert load_bench_repos(workdir["mapping"]) == {"a": "repolace/bench-a"}

    def test_a_second_run_changes_nothing(self, workdir, capsys):
        seed(workdir, "a", "b")
        github = FakeGithub()
        assert run_fork(workdir, github, RecordingPush(github.events)) == 0
        first = workdir["mapping"].read_text()
        capsys.readouterr()

        assert run_fork(workdir, github, RecordingPush(github.events)) == 0

        assert workdir["mapping"].read_text() == first
        assert "2 ready (0 created, 2 already existed)" in capsys.readouterr().out
        assert sorted(github.repos) == ["bench-a", "bench-b"]

    def test_a_422_that_is_not_already_exists_is_an_error_not_a_skip(self, workdir):
        seed(workdir, "a")
        github = FakeGithub(reject_create=["bench-a"])
        push = RecordingPush(github.events)

        assert run_fork(workdir, github, push) == 1

        assert push.calls == []
        assert not workdir["mapping"].exists() or load_bench_repos(workdir["mapping"]) == {}

    def test_a_pre_existing_public_repository_is_not_ready(self, workdir):
        seed(workdir, "a")
        github = FakeGithub(public=["bench-a"])
        push = RecordingPush(github.events)

        assert run_fork(workdir, github, push) == 1

        assert push.calls == []
        assert not any(event[0] == "PUT" for event in github.events)

    def test_failing_to_disable_actions_marks_the_repository_not_ready(self, workdir, capsys):
        seed(workdir, "a", "b")
        github = FakeGithub(fail_actions=["bench-a"])
        push = RecordingPush(github.events)

        assert run_fork(workdir, github, push) == 1

        # Nothing was pushed to the repository whose Actions could not be disabled,
        # and it is not in the mapping; the other repository is unaffected.
        assert [call[0] for call in push.calls] == [f"{GIT}/repolace/bench-b.git"]
        assert load_bench_repos(workdir["mapping"]) == {"b": "repolace/bench-b"}
        out = capsys.readouterr()
        assert "NOT READY repolace/bench-a" in out.out
        assert "1 not ready" in out.out

    def test_a_repository_that_stops_being_ready_leaves_the_mapping(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()
        assert run_fork(workdir, github, RecordingPush(github.events)) == 0
        assert load_bench_repos(workdir["mapping"]) == {"a": "repolace/bench-a"}

        github.fail_actions.add("bench-a")
        assert run_fork(workdir, github, RecordingPush(github.events)) == 1

        assert load_bench_repos(workdir["mapping"]) == {}

    def test_a_failed_push_marks_the_repository_not_ready(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()
        push = RecordingPush(github.events, fail_for=["bench-a"], error=GitCommandError(["push"], 1, "rejected"))

        assert run_fork(workdir, github, push) == 1

        assert load_bench_repos(workdir["mapping"]) == {}

    def test_installation_is_untouched_by_default(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()

        assert run_fork(workdir, github, RecordingPush(github.events)) == 0

        assert github.installation_adds == []
        assert not any(event[1].startswith("/user/") for event in github.events if event[0] != "PUSH")

    def test_add_to_installation_uses_the_id_of_the_verified_bench_repository(self, workdir):
        seed(workdir, "a")
        github = FakeGithub(existing=["repolace", "bench-a"])  # `repolace` takes id 1000, bench-a 1001

        assert run_fork(workdir, github, RecordingPush(github.events), "--add-to-installation", "77") == 0

        assert github.installation_adds == [(77, github.repos["bench-a"]["id"])]
        assert github.installation_adds[0][1] != github.repos["repolace"]["id"]

    def test_installation_id_must_be_positive(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()

        assert run_fork(workdir, github, RecordingPush(github.events), "--add-to-installation", "0") == 2
        assert github.requests == []

    def test_a_redirect_is_not_followed(self, workdir):
        seed(workdir, "a")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(422, json={"errors": [{"message": "name already exists on this account"}]})
            return httpx.Response(301, headers={"Location": "https://api.test/repos/repolace/repolace"})

        code = main(
            ["--instances-dir", str(workdir["instances"])],
            transport=httpx.MockTransport(handler), token_reader=lambda: TOKEN,
            git_push=RecordingPush(), checkout=fake_checkout, api_url=API, git_url=GIT,
        )

        assert code == 1
        assert all("/repos/repolace/repolace" != request.url.path for request in requests)

    def test_a_missing_token_is_a_usage_error_naming_the_variable(self, workdir, capsys):
        seed(workdir, "a")
        github = FakeGithub()

        assert run_fork(workdir, github, RecordingPush(), token=None) == 2

        assert TOKEN_ENV_VAR in capsys.readouterr().err
        assert github.requests == []

    def test_an_unknown_instance_is_a_usage_error(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()

        assert run_fork(workdir, github, RecordingPush(), "--instances", "nope") == 2
        assert github.requests == []

    def test_a_subset_touches_only_the_chosen_instances(self, workdir):
        seed(workdir, "a", "b")
        github = FakeGithub()

        assert run_fork(workdir, github, RecordingPush(github.events), "--instances", "b") == 0

        assert list(github.repos) == ["bench-b"]


class TestDelete:
    def prepare(self, workdir):
        seed(workdir, "a", "b")
        github = FakeGithub(existing=["repolace", "bench-a", "bench-b", "bench-unlisted"])
        workdir["mapping"].write_text('"a" = "repolace/bench-a"\n"b" = "repolace/bench-b"\n')
        return github

    def delete(self, workdir, github, *extra):
        return main(
            ["--instances-dir", str(workdir["instances"]), "--delete", *extra],
            transport=github.transport(), token_reader=lambda: TOKEN, api_url=API, git_url=GIT,
        )

    def test_without_yes_it_lists_and_deletes_nothing(self, workdir, capsys):
        github = self.prepare(workdir)

        assert self.delete(workdir, github) == 2

        assert github.requests == []
        assert "would delete repolace/bench-a" in capsys.readouterr().out

    def test_deletes_only_listed_bench_repositories(self, workdir):
        github = self.prepare(workdir)

        assert self.delete(workdir, github, "--yes") == 0

        assert sorted(github.repos) == ["bench-unlisted", "repolace"]
        deleted = [path for method, path in github.events if method == "DELETE"]
        assert deleted == ["/repos/repolace/bench-a", "/repos/repolace/bench-b"]
        assert load_bench_repos(workdir["mapping"]) == {}

    def test_an_instance_that_is_not_listed_is_refused_before_any_request(self, workdir):
        github = self.prepare(workdir)

        assert self.delete(workdir, github, "--yes", "--instances", "unlisted") == 2

        assert github.requests == []

    def test_a_repository_that_is_already_gone_is_not_an_error(self, workdir):
        github = self.prepare(workdir)
        del github.repos["bench-a"]

        assert self.delete(workdir, github, "--yes") == 0

        assert load_bench_repos(workdir["mapping"]) == {}

    def test_a_failed_delete_keeps_the_entry_and_exits_nonzero(self, workdir):
        github = self.prepare(workdir)
        github.fail_delete.add("bench-a")

        assert self.delete(workdir, github, "--yes") == 1

        assert load_bench_repos(workdir["mapping"]) == {"a": "repolace/bench-a"}
        assert "bench-b" not in github.repos

    def test_nothing_listed_is_a_clean_no_op(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()

        assert self.delete(workdir, github, "--yes") == 0
        assert github.requests == []


class TestTokenHygiene:
    """The credential reaches GitHub in one header and nowhere else."""

    def collect(self, workdir, capsys, caplog, github, push, *extra):
        caplog.set_level(logging.DEBUG)
        try:
            code = run_fork(workdir, github, push, *extra)
        except BaseException as exc:  # an escaped exception's text is part of the surface
            return None, str(exc) + repr(exc), capsys.readouterr(), caplog.text
        captured = capsys.readouterr()
        return code, "", captured, caplog.text

    def assert_only_in_authorization(self, github):
        for request in github.requests:
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            assert TOKEN not in str(request.url)
            assert TOKEN not in request.content.decode()
            assert all(TOKEN not in value for key, value in request.headers.items() if key != "authorization")

    def test_a_successful_run_never_prints_or_logs_it(self, workdir, capsys, caplog):
        seed(workdir, "a")
        github = FakeGithub()
        code, raised, captured, logged = self.collect(workdir, capsys, caplog, github, RecordingPush(github.events))

        assert code == 0
        assert TOKEN not in captured.out + captured.err + logged + raised
        self.assert_only_in_authorization(github)

    def test_a_server_that_echoes_the_credential_is_scrubbed_from_the_report(self, workdir, capsys, caplog):
        seed(workdir, "a")
        github = FakeGithub(fail_actions=["bench-a"], echo_credentials=True)
        code, raised, captured, logged = self.collect(workdir, capsys, caplog, github, RecordingPush(github.events))

        assert code == 1
        assert "NOT READY" in captured.out
        assert "<token>" in captured.out
        assert TOKEN not in captured.out + captured.err + logged + raised

    def test_a_failed_git_push_is_scrubbed(self, workdir, capsys, caplog):
        seed(workdir, "a")
        github = FakeGithub()
        error = GitCommandError(["push", "x"], 1, f"fatal: could not read from remote using {TOKEN}")
        code, raised, captured, logged = self.collect(
            workdir, capsys, caplog, github, RecordingPush(github.events, fail_for=["bench-a"], error=error)
        )

        assert code == 1
        assert TOKEN not in captured.out + captured.err + logged + raised

    def test_a_transport_error_that_carries_the_credential_is_scrubbed(self, workdir, capsys, caplog):
        seed(workdir, "a")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"tunnel failed for {request.headers['authorization']}")

        caplog.set_level(logging.DEBUG)
        code = main(
            ["--instances-dir", str(workdir["instances"])],
            transport=httpx.MockTransport(handler), token_reader=lambda: TOKEN,
            git_push=RecordingPush(), checkout=fake_checkout, api_url=API, git_url=GIT,
        )
        captured = capsys.readouterr()

        assert code == 1
        assert TOKEN not in captured.out + captured.err + caplog.text

    def test_an_unexpected_exception_is_scrubbed_rather_than_traced(self, workdir, capsys):
        seed(workdir, "a")

        class Boom(RuntimeError):
            pass

        async def push(url, refspec, checkout, token):
            raise Boom(f"unexpected failure holding {token}")

        code = main(
            ["--instances-dir", str(workdir["instances"])],
            transport=FakeGithub().transport(), token_reader=lambda: TOKEN,
            git_push=push, checkout=fake_checkout, api_url=API, git_url=GIT,
        )
        captured = capsys.readouterr()

        assert code == 1
        assert TOKEN not in captured.out + captured.err
        assert "Boom" in captured.err

    def test_there_is_no_token_flag(self, workdir):
        with pytest.raises(SystemExit) as exit_info:
            main(["--instances-dir", str(workdir["instances"]), "--token", TOKEN], token_reader=lambda: TOKEN)
        assert exit_info.value.code == 2

    def test_git_receives_the_token_through_the_environment_and_never_argv(self, tmp_path, monkeypatch):
        calls: list[tuple[tuple[str, ...], dict]] = []

        class FakeProcess:
            pid = 424242
            returncode = 0

            async def communicate(self):
                return b"", b""

            async def wait(self):
                return 0

        async def fake_spawn(*argv, **kwargs):
            calls.append((argv, kwargs))
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setenv(TOKEN_ENV_VAR, "other-sentinel-must-not-reach-git")

        # The autouse guard refuses any https URL; this test's spawn is faked, so use the real runner.
        monkeypatch.setattr(bench_repos, "run_git", shared_run_git)
        asyncio.run(
            bench_repos.push_with_git("https://github.com/repolace/bench-a.git", f"{BASE_COMMIT}:refs/heads/main", tmp_path, TOKEN)
        )

        (argv, kwargs), = calls
        assert "push" in argv and "https://github.com/repolace/bench-a.git" in argv
        assert not any(TOKEN in part for part in argv)
        assert "--force" not in argv and "-f" not in argv
        helper = next(part for part in argv if part.startswith("credential.helper=!"))
        assert "$REPOLACE_GIT_TOKEN" in helper  # the variable's name, expanded by the helper's shell
        env = kwargs["env"]
        assert env["REPOLACE_GIT_TOKEN"] == TOKEN
        assert env["REPOLACE_GIT_HOST"] == "github.com"
        assert TOKEN_ENV_VAR not in env
        assert "other-sentinel-must-not-reach-git" not in env.values()

    def test_only_this_module_reads_the_token_variable(self):
        """No other source file holds the variable's name as a string in code.

        Docstrings and comments may talk about it; an `os.environ[...]` or
        `os.getenv(...)` elsewhere would be a second reader, which is what this
        forbids. The runner strips it from the children by importing the constant.
        """
        root = Path(__file__).resolve().parents[2]
        offenders = []
        for path in root.rglob("*.py"):
            parts = set(path.relative_to(root).parts)
            if parts & {".venv", "tests", "node_modules", "__pycache__"} or path.name.startswith("test_"):
                continue
            # Data, not source: the clones `select` keeps of upstream repositories (which
            # carry deliberately broken Python as test fixtures) and the runner's logs.
            if path.relative_to(root).parts[:2] in {("eval", "cache"), ("eval", "runs")}:
                continue
            if path.name == "bench_repos.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and TOKEN_ENV_VAR in node.value and id(node) not in docstrings
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
        assert offenders == []


class TestUpstreamCheckout:
    def test_a_usable_cache_is_used_without_cloning(self, tmp_path):
        cache = tmp_path / "cache"
        sha = make_cached_upstream(cache)
        spec = make_instance("a", base_commit=sha)

        async def go():
            async with upstream_checkout(spec, cache) as checkout:
                return checkout

        assert asyncio.run(go()) == (cache / "psf__requests").resolve()

    @pytest.mark.parametrize("repo", ["../x", "a/b/c", "a b/c", "psf", "psf/", "/etc/passwd", "a/.."])
    def test_a_malformed_upstream_name_is_refused(self, tmp_path, repo):
        spec = make_instance("a", repo=repo)

        async def go():
            async with upstream_checkout(spec, tmp_path):
                pass

        with pytest.raises(BenchRepoError):
            asyncio.run(go())

    def test_a_cache_missing_the_commit_falls_back_to_a_full_clone_of_the_upstream(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache"
        make_cached_upstream(cache)
        spec = make_instance("a")  # BASE_COMMIT is not in the cached repository
        cloned: list[tuple[str, ...]] = []
        real = bench_repos.run_git

        async def fake_run_git(*args, **kwargs):
            if args[0] == "clone":
                cloned.append(args)
                raise GitCommandError(list(args), 128, "no network in tests")
            return await real(*args, **kwargs)

        monkeypatch.setattr(bench_repos, "run_git", fake_run_git)

        async def go():
            async with upstream_checkout(spec, cache):
                pass

        with pytest.raises(GitCommandError):
            asyncio.run(go())
        assert cloned and "https://github.com/psf/requests.git" in cloned[0]
        assert "--depth" not in cloned[0]
        assert "--no-checkout" in cloned[0], "only history is needed; the hostile tree must not be checked out on the host"

    def test_a_shallow_cache_is_not_used(self, tmp_path, monkeypatch):
        source = tmp_path / "source"
        source.mkdir()
        run_git_sync("init", "-q", "-b", "main", cwd=source)
        for index in range(2):
            (source / "f").write_text(str(index))
            run_git_sync("add", "f", cwd=source)
            run_git_sync("commit", "-q", "-m", str(index), cwd=source)
        head = run_git_sync("rev-parse", "HEAD", cwd=source)
        cache = tmp_path / "cache"
        cache.mkdir()
        run_git_sync("clone", "-q", "--depth", "1", f"file://{source}", str(cache / "psf__requests"), cwd=tmp_path)
        spec = make_instance("a", base_commit=head)
        cloned: list[tuple[str, ...]] = []
        real = bench_repos.run_git

        async def fake_run_git(*args, **kwargs):
            if args[0] == "clone":
                cloned.append(args)
                raise GitCommandError(list(args), 128, "no network in tests")
            return await real(*args, **kwargs)

        monkeypatch.setattr(bench_repos, "run_git", fake_run_git)

        async def go():
            async with upstream_checkout(spec, cache):
                pass

        with pytest.raises(GitCommandError):
            asyncio.run(go())
        assert cloned, "a shallow cache must be skipped in favour of a full clone"


class TestRealGitPush:
    def test_the_base_commit_lands_on_main_of_the_bench_repository(self, workdir):
        """The shipped push path against local repositories: no network, no seam."""
        sha = make_cached_upstream(workdir["cache"])
        write_instances(workdir["instances"], make_instance("a", base_commit=sha))
        remote_root = workdir["root"] / "remote"
        bare = remote_root / "repolace" / "bench-a.git"
        bare.mkdir(parents=True)
        run_git_sync("init", "-q", "--bare", "-b", "main", cwd=bare)
        github = FakeGithub()

        code = main(
            ["--instances-dir", str(workdir["instances"]), "--cache-dir", str(workdir["cache"])],
            transport=github.transport(), token_reader=lambda: TOKEN, api_url=API, git_url=f"file://{remote_root}",
        )

        assert code == 0
        assert run_git_sync("rev-parse", "refs/heads/main", cwd=bare) == sha

    def test_only_the_base_commit_reaches_the_remote_whatever_else_the_upstream_has(self, workdir):
        """A tag, another branch and later commits upstream must not follow the base commit.

        The later commits include the fix. Pushed, they would put the answer into the
        benchmark repository the agent works in.
        """
        base = make_cached_upstream(workdir["cache"])
        cache = workdir["cache"] / "psf__requests"
        run_git_sync("tag", "v1", cwd=cache)
        run_git_sync("checkout", "-q", "-b", "other", cwd=cache)
        (cache / "other.txt").write_text("a branch the benchmark must not see\n")
        run_git_sync("add", "other.txt", cwd=cache)
        run_git_sync("commit", "-q", "-m", "on another branch", cwd=cache)
        other = run_git_sync("rev-parse", "HEAD", cwd=cache)
        run_git_sync("checkout", "-q", "main", cwd=cache)
        (cache / "README").write_text("the fix\n")
        run_git_sync("commit", "-q", "-am", "the fix", cwd=cache)
        fix = run_git_sync("rev-parse", "HEAD", cwd=cache)
        write_instances(workdir["instances"], make_instance("a", base_commit=base))
        remote_root = workdir["root"] / "remote"
        bare = remote_root / "repolace" / "bench-a.git"
        bare.mkdir(parents=True)
        run_git_sync("init", "-q", "--bare", "-b", "main", cwd=bare)

        code = main(
            ["--instances-dir", str(workdir["instances"]), "--cache-dir", str(workdir["cache"])],
            transport=FakeGithub().transport(), token_reader=lambda: TOKEN, api_url=API, git_url=f"file://{remote_root}",
        )

        assert code == 0
        assert run_git_sync("for-each-ref", "--format=%(refname) %(objectname)", cwd=bare) == f"refs/heads/main {base}"
        assert run_git_sync("rev-list", "--all", cwd=bare) == base, "only the base commit's history, nothing newer"
        for later in (fix, other):
            assert subprocess.run(["git", "cat-file", "-e", later], cwd=bare).returncode != 0, "a later commit was pushed"

    def test_a_second_push_of_the_same_commit_succeeds(self, workdir):
        sha = make_cached_upstream(workdir["cache"])
        write_instances(workdir["instances"], make_instance("a", base_commit=sha))
        remote_root = workdir["root"] / "remote"
        bare = remote_root / "repolace" / "bench-a.git"
        bare.mkdir(parents=True)
        run_git_sync("init", "-q", "--bare", "-b", "main", cwd=bare)
        github = FakeGithub()

        for _ in range(2):
            assert main(
                ["--instances-dir", str(workdir["instances"]), "--cache-dir", str(workdir["cache"])],
                transport=github.transport(), token_reader=lambda: TOKEN, api_url=API, git_url=f"file://{remote_root}",
            ) == 0

    def test_a_remote_whose_main_has_diverged_is_refused_not_overwritten(self, workdir, capsys):
        sha = make_cached_upstream(workdir["cache"])
        write_instances(workdir["instances"], make_instance("a", base_commit=sha))
        remote_root = workdir["root"] / "remote"
        bare = remote_root / "repolace" / "bench-a.git"
        bare.mkdir(parents=True)
        run_git_sync("init", "-q", "--bare", "-b", "main", cwd=bare)
        other = workdir["root"] / "other"
        other.mkdir()
        run_git_sync("init", "-q", "-b", "main", cwd=other)
        (other / "x").write_text("unrelated")
        run_git_sync("add", "x", cwd=other)
        run_git_sync("commit", "-q", "-m", "unrelated", cwd=other)
        run_git_sync("push", "-q", str(bare), "main:main", cwd=other)
        unrelated = run_git_sync("rev-parse", "main", cwd=other)

        code = main(
            ["--instances-dir", str(workdir["instances"]), "--cache-dir", str(workdir["cache"])],
            transport=FakeGithub().transport(), token_reader=lambda: TOKEN, api_url=API, git_url=f"file://{remote_root}",
        )

        assert code == 1
        assert run_git_sync("rev-parse", "refs/heads/main", cwd=bare) == unrelated
        assert "NOT READY" in capsys.readouterr().out


def one_run_main(workdir, github, *extra, token=TOKEN):
    """`fork --delete ...` against a fake GitHub."""
    return main(
        ["--instances-dir", str(workdir["instances"]), "--delete", *extra],
        transport=github.transport(), token_reader=lambda: token, api_url=API, git_url=GIT,
    )


class TestOnlyWhatThisToolCreated:
    """A 422 "already exists" must not adopt a repository someone else made, and `--delete`
    must not delete one: the marker the tool puts on every repository it creates is the only
    evidence of ownership, checked where the GET and the DELETE are sent."""

    def test_the_marker_is_what_creation_sends(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()

        assert run_fork(workdir, github, RecordingPush(github.events)) == 0

        assert github.bodies[0]["description"] == BENCH_MARKER
        assert github.repos["bench-a"]["description"] == BENCH_MARKER

    def test_a_pre_existing_repository_without_the_marker_is_refused_and_left_untouched(self, workdir, capsys):
        seed(workdir, "a")
        github = FakeGithub(foreign=["bench-a"])
        push = RecordingPush(github.events)

        assert run_fork(workdir, github, push) == 1

        assert push.calls == []
        assert not any(event[0] == "PUT" for event in github.events), "no Actions change, no installation change"
        assert github.repos["bench-a"]["actions_disabled"] is False
        assert "exists but was not created by this tool" in capsys.readouterr().out
        assert load_bench_entries(workdir["mapping"]) == {}, "a repository that is not ours is never recorded"

    def test_a_marked_repository_that_is_public_is_refused_on_create(self, workdir):
        seed(workdir, "a")
        github = FakeGithub(public=["bench-a"])
        push = RecordingPush(github.events)

        assert run_fork(workdir, github, push) == 1
        assert push.calls == [] and load_bench_entries(workdir["mapping"]) == {}

    def test_a_repository_that_becomes_someone_elses_is_dropped_from_the_record(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()
        assert run_fork(workdir, github, RecordingPush(github.events)) == 0
        github.repos["bench-a"]["description"] = "replaced by somebody else"

        assert run_fork(workdir, github, RecordingPush(github.events)) == 1

        assert load_bench_entries(workdir["mapping"]) == {}

    def test_the_installation_step_refuses_a_repository_without_the_marker(self):
        github = FakeGithub(foreign=["bench-a"])

        async def attempt():
            client = GithubBench(TOKEN, base_url=API, transport=github.transport())
            try:
                await client.add_to_installation(7, BenchRepo("bench-a"))
            finally:
                await client.aclose()

        with pytest.raises(NotCreatedByThisTool):
            asyncio.run(attempt())
        assert github.installation_adds == []

    def listed(self, workdir, status="ready"):
        if status == "ready":
            workdir["mapping"].write_text('"a" = "repolace/bench-a"\n')
        else:
            workdir["mapping"].write_text('"a" = { repo = "repolace/bench-a", status = "incomplete" }\n')

    def test_delete_refuses_a_listed_repository_without_the_marker(self, workdir, capsys):
        self.listed(workdir)
        github = FakeGithub(foreign=["bench-a"])

        assert one_run_main(workdir, github, "--yes") == 1

        assert not any(event[0] == "DELETE" for event in github.events), "no DELETE may be sent"
        assert "bench-a" in github.repos
        assert "not created by this tool" in capsys.readouterr().out
        assert "a" in load_bench_entries(workdir["mapping"]), "the entry stays: nothing was deleted"

    def test_delete_checks_at_the_moment_of_the_delete_not_when_the_entry_was_written(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()
        assert run_fork(workdir, github, RecordingPush(github.events)) == 0  # created by the tool, listed
        github.repos["bench-a"]["description"] = "swapped afterwards"

        assert one_run_main(workdir, github, "--yes") == 1

        assert "bench-a" in github.repos
        assert not any(event[0] == "DELETE" for event in github.events)

    def test_delete_refuses_a_marked_repository_that_is_public(self, workdir):
        self.listed(workdir)
        github = FakeGithub(public=["bench-a"])

        assert one_run_main(workdir, github, "--yes") == 1

        assert not any(event[0] == "DELETE" for event in github.events)

    def test_a_repository_this_tool_created_is_looked_up_and_then_deleted(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()
        assert run_fork(workdir, github, RecordingPush(github.events)) == 0
        github.events.clear()

        assert one_run_main(workdir, github, "--yes") == 0

        assert github.events == [("GET", "/repos/repolace/bench-a"), ("DELETE", "/repos/repolace/bench-a")]
        assert "bench-a" not in github.repos
        assert load_bench_entries(workdir["mapping"]) == {}

    def test_repeated_ids_are_deleted_once_and_the_rest_still_are(self, workdir, capsys):
        seed(workdir, "a", "b")
        github = FakeGithub()
        assert run_fork(workdir, github, RecordingPush(github.events)) == 0
        github.events.clear()

        assert one_run_main(workdir, github, "--yes", "--instances", "a,a,b,a") == 0

        assert [event for event in github.events if event[0] == "DELETE"] == [
            ("DELETE", "/repos/repolace/bench-a"), ("DELETE", "/repos/repolace/bench-b"),
        ]
        out = capsys.readouterr().out
        assert out.count("deleting repolace/bench-a") == 1


class TestDeleteReposDirectly:
    def test_repeated_ids_handed_to_delete_repos_are_deleted_once_not_a_keyerror(self, workdir):
        """The CLI dedupes `--instances`, but the function is called without it too."""
        seed(workdir, "a", "b")
        github = FakeGithub()
        assert run_fork(workdir, github, RecordingPush(github.events)) == 0
        output: list[str] = []

        async def go():
            client = GithubBench(TOKEN, base_url=API, transport=github.transport())
            try:
                return await bench_repos.delete_repos(
                    ["a", "a", "b", "a"], client, mapping_path=workdir["mapping"], out=output.append
                )
            finally:
                await client.aclose()

        failures = asyncio.run(go())

        assert failures == 0 and github.repos == {}
        assert output == ["deleted repolace/bench-a", "deleted repolace/bench-b"]


class TestIncompleteRepositories:
    """A repository this tool created and then failed on must stay deletable."""

    def test_a_failed_actions_call_records_the_repository_as_incomplete(self, workdir):
        seed(workdir, "a")
        github = FakeGithub(fail_actions=["bench-a"])

        assert run_fork(workdir, github, RecordingPush(github.events)) == 1

        assert load_bench_entries(workdir["mapping"])["a"].status == "incomplete"
        assert load_bench_repos(workdir["mapping"]) == {}, "an incomplete repository is not a ready one"
        assert 'status = "incomplete"' in workdir["mapping"].read_text()

    def test_a_failed_push_records_it_as_incomplete(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()
        push = RecordingPush(github.events, fail_for=["bench-a"], error=GitCommandError(["push"], 1, "rejected"))

        assert run_fork(workdir, github, push) == 1

        assert load_bench_entries(workdir["mapping"])["a"].status == "incomplete"

    def test_a_failed_installation_step_records_it_as_incomplete(self, workdir):
        seed(workdir, "a")
        github = FakeGithub()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/user/installations"):
                return httpx.Response(403, json={"message": "forbidden"})
            return github._handle(request)

        code = main(
            ["--instances-dir", str(workdir["instances"]), "--cache-dir", str(workdir["cache"]), "--add-to-installation", "5"],
            transport=httpx.MockTransport(handler), token_reader=lambda: TOKEN, git_push=RecordingPush(github.events),
            checkout=fake_checkout, api_url=API, git_url=GIT,
        )

        assert code == 1
        assert load_bench_entries(workdir["mapping"])["a"].status == "incomplete"

    def test_delete_lists_and_removes_an_incomplete_repository(self, workdir, capsys):
        seed(workdir, "a")
        github = FakeGithub(fail_actions=["bench-a"])
        assert run_fork(workdir, github, RecordingPush(github.events)) == 1
        capsys.readouterr()

        assert one_run_main(workdir, github) == 2  # without --yes it only lists
        assert "would delete repolace/bench-a (incomplete)" in capsys.readouterr().out
        assert one_run_main(workdir, github, "--yes") == 0

        assert "bench-a" not in github.repos
        assert load_bench_entries(workdir["mapping"]) == {}

    def test_a_later_successful_run_makes_it_ready(self, workdir):
        seed(workdir, "a")
        github = FakeGithub(fail_actions=["bench-a"])
        assert run_fork(workdir, github, RecordingPush(github.events)) == 1
        github.fail_actions.clear()

        assert run_fork(workdir, github, RecordingPush(github.events)) == 0

        assert load_bench_entries(workdir["mapping"])["a"].status == "ready"

    def test_a_failure_before_the_repository_exists_records_nothing(self, workdir):
        seed(workdir, "a")
        github = FakeGithub(reject_create=["bench-a"])

        assert run_fork(workdir, github, RecordingPush(github.events)) == 1

        assert load_bench_entries(workdir["mapping"]) == {}

    @pytest.mark.parametrize(
        "text",
        [
            '"a" = { repo = "repolace/bench-a", status = "weird" }\n',
            '"a" = { repo = "repolace/bench-a" }\n',
            '"a" = { repo = "repolace/bench-a", status = "incomplete", extra = 1 }\n',
            '"a" = { repo = "repolace/repolace", status = "incomplete" }\n',
            '"a" = { repo = "repolace/bench-b", status = "incomplete" }\n',
        ],
    )
    def test_a_malformed_or_redirected_entry_is_refused(self, workdir, text):
        workdir["mapping"].write_text(text)

        with pytest.raises(BenchRepoError):
            load_bench_entries(workdir["mapping"])


class TestPushIsPinned:
    """One commit to one branch: nothing else the upstream has may reach a benchmark repository."""

    GOOD = f"{BASE_COMMIT}:refs/heads/main"

    @pytest.mark.parametrize(
        "url",
        ["https://github.com/repolace/bench-a.git", "file:///tmp/remote/repolace/bench-a.git"],
    )
    def test_the_expected_pushes_are_accepted(self, url):
        check_push_target(url, self.GOOD)

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/repolace/repolace.git",
            "https://github.com/psf/requests.git",
            "https://github.com/repolace/bench-a",
            "https://x:secret@github.com/repolace/bench-a.git",
            "https://github.com/repolace/bench-a.git?x=1",
            "https://github.com/repolace/bench-a.git#frag",
            "http://github.com/repolace/bench-a.git",
            "ssh://git@github.com/repolace/bench-a.git",
            "git://github.com/repolace/bench-a.git",
            "https://github.com/evil/repolace/bench-a.git",
            "https://github.com/repolace/bench-a/../repolace.git",
            "https://github.com/repolace/bench-a.git/extra",
            "https:///repolace/bench-a.git",
            "file:///tmp/bench-a.git",
            "/tmp/remote/repolace/bench-a.git",
            "repolace/bench-a.git",
            "",
        ],
    )
    def test_a_push_to_anywhere_else_is_refused(self, url):
        with pytest.raises(BenchRepoError):
            check_push_target(url, self.GOOD)

    @pytest.mark.parametrize(
        "refspec",
        [
            f"+{BASE_COMMIT}:refs/heads/main",
            f"{BASE_COMMIT}:refs/heads/other",
            f"{BASE_COMMIT}:refs/tags/v1",
            f"{BASE_COMMIT}:refs/heads/main\n",
            f" {BASE_COMMIT}:refs/heads/main",
            f"{BASE_COMMIT.upper()}:refs/heads/main",
            f"{BASE_COMMIT[:12]}:refs/heads/main",
            "HEAD:refs/heads/main",
            "refs/heads/main:refs/heads/main",
            "--all",
            "--tags",
            "--mirror",
            "",
            f"{BASE_COMMIT}:refs/heads/main {BASE_COMMIT}:refs/heads/x",
        ],
    )
    def test_any_other_refspec_is_refused(self, refspec):
        with pytest.raises(BenchRepoError):
            check_push_target("https://github.com/repolace/bench-a.git", refspec)

    def test_the_shipped_push_refuses_before_spawning_anything(self, tmp_path, monkeypatch):
        spawned = []

        async def fail_spawn(*argv, **kwargs):
            spawned.append(argv)
            raise AssertionError("git must not be spawned for a refused push")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
        monkeypatch.setattr(bench_repos, "run_git", shared_run_git)

        for url, refspec in (
            ("https://github.com/repolace/repolace.git", self.GOOD),
            ("https://github.com/repolace/bench-a.git", "--all"),
            ("https://github.com/repolace/bench-a.git", "--tags"),
        ):
            with pytest.raises(BenchRepoError):
                asyncio.run(bench_repos.push_with_git(url, refspec, tmp_path, TOKEN))
        assert spawned == []


class TestApiErrorScrubbing:
    def test_a_token_straddling_the_truncation_point_leaves_no_prefix(self):
        """Scrubbing a body already cut at 300 characters leaves the start of a token cut in half."""
        body = "x" * 292 + TOKEN + " trailing detail"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text=body)

        async def attempt():
            client = GithubBench(TOKEN, base_url=API, transport=httpx.MockTransport(handler))
            try:
                await client.disable_actions(BenchRepo("bench-a"))
            finally:
                await client.aclose()

        with pytest.raises(BenchRepoError) as raised:
            asyncio.run(attempt())

        message = str(raised.value)
        assert "benchtok" not in message and TOKEN not in message
        assert "x" * 100 in message, "the error must still carry the start of the body"

    def test_a_full_name_mismatch_in_the_response_is_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 1, "full_name": "repolace/repolace", "private": True})

        async def attempt():
            client = GithubBench(TOKEN, base_url=API, transport=httpx.MockTransport(handler))
            try:
                await client.get_repo(BenchRepo("bench-a"))
            finally:
                await client.aclose()

        with pytest.raises(BenchRepoError, match="asked for repolace/bench-a but GitHub returned"):
            asyncio.run(attempt())


class TestUpstreamClone:
    def spawn_recorder(self, monkeypatch):
        calls = []

        class FakeProcess:
            pid = 31337
            returncode = 0

            def __init__(self, stdout=b""):
                self._stdout = stdout

            async def communicate(self):
                return self._stdout, b""

            async def wait(self):
                return 0

        async def fake_spawn(*argv, **kwargs):
            calls.append((argv, kwargs))
            return FakeProcess(b"false\n" if "--is-shallow-repository" in argv else b"")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(bench_repos, "run_git", shared_run_git)
        return calls

    def test_the_upstream_clone_carries_no_credential_at_all(self, tmp_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "other-sentinel-must-not-reach-git")
        calls = self.spawn_recorder(monkeypatch)
        spec = make_instance("a")

        async def go():
            async with upstream_checkout(spec, tmp_path / "no-cache"):
                pass

        asyncio.run(go())

        argv, kwargs = next(call for call in calls if "clone" in call[0])
        assert "https://github.com/psf/requests.git" in argv and "--no-checkout" in argv
        assert not any("@" in part and "://" in part for part in argv), "no credentials in the URL"
        assert not any(TOKEN in part for part in argv)
        env = kwargs["env"]
        assert env["REPOLACE_GIT_TOKEN"] == "", "the clone is made without a token"
        assert TOKEN_ENV_VAR not in env
        assert "other-sentinel-must-not-reach-git" not in env.values()

    def test_a_symlinked_cache_entry_is_refused_rather_than_followed(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        sha = make_cached_upstream(elsewhere)
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "psf__requests").symlink_to(elsewhere / "psf__requests")
        spec = make_instance("a", base_commit=sha)

        async def go():
            async with upstream_checkout(spec, cache):
                pass

        with pytest.raises(BenchRepoError, match="symlink"):
            asyncio.run(go())

    def test_a_real_no_checkout_clone_pushes_only_the_base_commit(self, workdir, monkeypatch):
        """Real git end to end, no cache: the temp clone has history but no working tree."""
        upstream_root = workdir["root"] / "upstream-root"
        upstream = upstream_root / "psf" / "requests.git"  # a non-bare repository named like a GitHub URL
        upstream.mkdir(parents=True)
        run_git_sync("init", "-q", "-b", "main", cwd=upstream)
        (upstream / "README").write_text("upstream\n")
        run_git_sync("add", "README", cwd=upstream)
        run_git_sync("commit", "-q", "-m", "base", cwd=upstream)
        base = run_git_sync("rev-parse", "HEAD", cwd=upstream)
        run_git_sync("tag", "v1", cwd=upstream)
        (upstream / "README").write_text("fixed\n")
        run_git_sync("commit", "-q", "-am", "the fix", cwd=upstream)
        fix = run_git_sync("rev-parse", "HEAD", cwd=upstream)
        monkeypatch.setattr(bench_repos, "GITHUB_GIT", f"file://{upstream_root}")
        write_instances(workdir["instances"], make_instance("a", base_commit=base))
        remote_root = workdir["root"] / "remote"
        bare = remote_root / "repolace" / "bench-a.git"
        bare.mkdir(parents=True)
        run_git_sync("init", "-q", "--bare", "-b", "main", cwd=bare)
        seen_checkouts = []
        real_checkout = upstream_checkout

        import contextlib

        @contextlib.asynccontextmanager
        async def spying(spec, cache_dir):
            async with real_checkout(spec, cache_dir) as path:
                seen_checkouts.append(sorted(p.name for p in path.iterdir()))
                yield path

        code = main(
            ["--instances-dir", str(workdir["instances"]), "--cache-dir", str(workdir["cache"])],
            transport=FakeGithub().transport(), token_reader=lambda: TOKEN, checkout=spying, api_url=API,
            git_url=f"file://{remote_root}",
        )

        assert code == 0
        assert seen_checkouts == [[".git"]], "no working-tree file may be written on the host"
        assert run_git_sync("rev-parse", "refs/heads/main", cwd=bare) == base
        assert run_git_sync("for-each-ref", "--format=%(refname)", cwd=bare) == "refs/heads/main"
        assert subprocess.run(["git", "cat-file", "-e", fix], cwd=bare).returncode != 0, "the fix commit must not be pushed"
