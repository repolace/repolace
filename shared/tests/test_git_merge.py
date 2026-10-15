"""A target branch that moved after the clone: detection, merge and commit primitives.

Real local bare remotes throughout (see conftest `source_repo`/`origin`), as in
`TestAgentFlow`. The security tests follow the hostile-hook style of
`test_git_repo.py`: plant something executable in the checkout's own `.git`,
run the operation, assert the sentinel never appeared.
"""

import dataclasses
import http.server
import subprocess
import threading
import uuid
from pathlib import Path
from typing import ClassVar

import pytest
from repolace_shared.git import repo as repo_module
from repolace_shared.git.repo import (
    CONFLICT_BOTH_ADDED,
    CONFLICT_BOTH_MODIFIED,
    CONFLICT_DELETE_MODIFY,
    CONFLICT_GITLINK,
    CONFLICT_MODE,
    CONFLICT_SYMLINK,
    MAX_UNMERGED_PATHS,
    GitError,
    GitVersionError,
    UntrustedConfigError,
    conflict_kind,
    parse_merge_tree,
    parse_unmerged,
    require_git_version,
    run_git_checked,
)
from repolace_shared.git.workspace import MAX_RESOLUTION_PATHS, task_workspace
from shared_support import git, write

pytestmark = pytest.mark.anyio

TASK_ID = uuid.UUID("2f8a1c4e-0000-4000-8000-000000000002")
TOKEN = "ghs_exampletokenvalue0123456789"


def workspace_for(origin_url: str, branch: str = "main", **kwargs):
    return task_workspace("acme", "sample", target_branch=branch, clone_url=origin_url, **kwargs)


@pytest.fixture
def push_upstream(tmp_path: Path, origin_url: str):
    """Land a commit on the remote's branch after the task cloned it."""
    counter = iter(range(1000))

    def push(files: dict[str, str | None], message: str = "upstream", branch: str = "main") -> str:
        work = tmp_path / f"upstream-{next(counter)}"
        git(tmp_path, "clone", "-q", "--branch", branch, origin_url, str(work))
        for name, content in files.items():
            if content is None:
                git(work, "rm", "-q", name)
            else:
                write(work / name, content)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", message)
        git(work, "push", "-q", "origin", branch)
        return git(work, "rev-parse", "HEAD")

    return push


APP_UPSTREAM = "def add(a, b):\n    return a * b\n"
APP_AGENT = "def add(a, b):\n    return a + b\n"


class TestParsing:
    def test_merge_tree_clean_output_has_no_paths(self):
        result = parse_merge_tree(b"a" * 40 + b"\0", clean=True)
        assert result.clean and result.conflicted_paths == ()

    def test_merge_tree_stops_at_the_empty_field_so_messages_are_not_paths(self):
        raw = b"a" * 40 + b"\0f\0r\0\0" + b"1\0f\0Auto-merging\0Auto-merging f\n\0"
        result = parse_merge_tree(raw, clean=False)
        assert result.conflicted_paths == (b"f", b"r")

    def test_a_non_utf8_path_stays_bytes(self):
        raw = b"b" * 40 + b"\0caf\xe9.py\0\0"
        assert parse_merge_tree(raw, clean=False).conflicted_paths == (b"caf\xe9.py",)

    def test_garbage_is_refused(self):
        with pytest.raises(GitError):
            parse_merge_tree(b"not a tree", clean=False)

    def test_unmerged_records_group_by_path(self):
        def rec(stage: int, digit: bytes, path: bytes) -> bytes:
            return b"100644 " + digit * 40 + b" " + str(stage).encode() + b"\t" + path + b"\x00"

        raw = (
            rec(1, b"1", b"f") + rec(2, b"2", b"f") + rec(3, b"3", b"f")
            + rec(1, b"4", b"r") + rec(2, b"5", b"r")
        )
        entries = {e.path: e.stages for e in parse_unmerged(raw)}
        assert entries == {b"f": {1: "100644", 2: "100644", 3: "100644"}, b"r": {1: "100644", 2: "100644"}}

    @pytest.mark.parametrize(
        ("stages", "kind"),
        [
            ({1: "100644", 2: "100644", 3: "100644"}, CONFLICT_BOTH_MODIFIED),
            ({2: "100644", 3: "100644"}, CONFLICT_BOTH_ADDED),
            ({1: "100644", 2: "100644"}, CONFLICT_DELETE_MODIFY),
            ({1: "100644", 3: "100644"}, CONFLICT_DELETE_MODIFY),
            ({1: "100644", 2: "100755", 3: "100644"}, CONFLICT_MODE),
            ({1: "100644", 2: "120000", 3: "100644"}, CONFLICT_SYMLINK),
            ({1: "100644", 2: "160000", 3: "100644"}, CONFLICT_GITLINK),
        ],
    )
    def test_conflict_kinds(self, stages, kind):
        assert conflict_kind(stages) == kind


class TestExitCodes:
    async def test_the_default_still_raises_on_any_non_zero_exit(self, source_repo):
        with pytest.raises(Exception, match="failed"):
            await run_git_checked("rev-parse", "--verify", "--quiet", "no-such-ref", cwd=source_repo)

    async def test_an_accepted_code_is_returned_and_any_other_still_raises(self, source_repo):
        code, _ = await run_git_checked(
            "rev-parse", "--verify", "--quiet", "no-such-ref", cwd=source_repo, ok_codes=frozenset({0, 1})
        )
        assert code == 1
        with pytest.raises(Exception, match="failed"):
            # Without --quiet git exits 128 for a missing ref: outside {0, 1}, so it raises.
            await run_git_checked("rev-parse", "--verify", "no-such-ref", cwd=source_repo,
                                  ok_codes=frozenset({0, 1}))


class TestVersionCheck:
    async def test_an_old_git_is_refused_with_a_clear_message(self, monkeypatch):
        monkeypatch.setattr(repo_module, "_git_version_cache", (2, 30))
        with pytest.raises(GitVersionError, match=r"git >= 2\.38"):
            await require_git_version()

    async def test_the_installed_git_passes(self, monkeypatch):
        monkeypatch.setattr(repo_module, "_git_version_cache", None)
        await require_git_version()


class TestDetection:
    async def test_an_unchanged_target_has_not_moved(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            moved, tip = await workspace.target_moved()
            assert not moved
            assert tip == workspace.base_sha

    async def test_a_non_conflicting_upstream_commit_merges_clean(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            write(workspace.path / "src" / "app.py", APP_AGENT)
            await workspace.record_attempt("fix")
            upstream = push_upstream({"README.md": "# sample\nmore\n"})

            moved, tip = await workspace.target_moved()
            assert moved and tip == upstream

            result = await workspace.repo.merge_tree("HEAD", tip)
            assert result.clean and result.conflicted_paths == ()
            # merge-tree wrote objects only: the checkout is untouched.
            assert await workspace.repo.tree_matches_head()
            assert not await workspace.repo.merge_in_progress()

    async def test_the_fetch_keeps_the_clone_non_shallow(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            push_upstream({"README.md": "# sample\nmore\n"})
            await workspace.target_moved()
            assert not await workspace.repo.is_shallow()

    async def test_a_conflicting_upstream_commit_reports_the_path(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            write(workspace.path / "src" / "app.py", APP_AGENT)
            await workspace.record_attempt("fix")
            push_upstream({"src/app.py": APP_UPSTREAM})

            _, tip = await workspace.target_moved()
            result = await workspace.repo.merge_tree("HEAD", tip)

            assert not result.clean
            assert result.conflicted_paths == (b"src/app.py",)

    async def test_a_rev_parse_that_is_not_a_commit_raises(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            with pytest.raises(GitError):
                await workspace.repo.rev_parse("no-such-ref")

    async def test_rev_parse_returns_a_full_sha(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            assert await workspace.repo.rev_parse("HEAD") == workspace.base_sha
            assert len(workspace.base_sha) == 40


class TestMergeAndResolve:
    async def _conflicted(self, workspace, push_upstream):
        await workspace.start_agent_branch(1, TASK_ID)
        write(workspace.path / "src" / "app.py", APP_AGENT)
        await workspace.record_attempt("fix")
        upstream = push_upstream({"src/app.py": APP_UPSTREAM})
        _, tip = await workspace.target_moved()
        assert tip == upstream
        return tip

    async def test_a_both_modified_conflict_is_classified_and_has_markers(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            tip = await self._conflicted(workspace, push_upstream)

            assert await workspace.repo.merge_no_commit(tip) is False

            (entry,) = await workspace.repo.unmerged_files()
            assert entry.path == b"src/app.py"
            assert conflict_kind(entry.stages) == CONFLICT_BOTH_MODIFIED
            text = (workspace.path / "src" / "app.py").read_text()
            assert "<<<<<<<" in text and "|||||||" in text, "zdiff3 style should carry the base"

    async def test_a_clean_merge_stops_before_committing(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            write(workspace.path / "src" / "app.py", APP_AGENT)
            await workspace.record_attempt("fix")
            push_upstream({"README.md": "# sample\nmore\n"})
            _, tip = await workspace.target_moved()
            head_before = await workspace.repo.head_sha()

            assert await workspace.repo.merge_no_commit(tip) is True

            assert await workspace.repo.head_sha() == head_before
            assert await workspace.repo.merge_in_progress()
            assert await workspace.repo.unmerged_files() == []

    async def test_abort_restores_the_tree(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            tip = await self._conflicted(workspace, push_upstream)
            await workspace.repo.merge_no_commit(tip)
            await workspace.repo.abort_merge()
            assert await workspace.repo.tree_matches_head()
            assert not await workspace.repo.merge_in_progress()

    async def test_resolve_commit_advance_base_and_review_diff_is_against_the_new_base(
        self, origin_url, push_upstream
    ):
        async with workspace_for(origin_url) as workspace:
            old_base = workspace.base_sha
            tip = await self._conflicted(workspace, push_upstream)
            await workspace.repo.merge_no_commit(tip)

            write(workspace.path / "src" / "app.py", "def add(a, b):\n    return a + b  # resolved\n")
            sha = await workspace.commit_resolution(["src/app.py"], "Merge target into agent branch")

            assert await workspace.repo.head_sha() == sha
            assert not await workspace.repo.merge_in_progress()
            parents = git(workspace.path, "rev-list", "--parents", "-n1", "HEAD").split()[1:]
            assert tip in parents and len(parents) == 2

            await workspace.advance_base(tip)
            assert workspace.base_sha == tip != old_base

            diff = await workspace.review_diff()
            assert "# resolved" in diff
            assert "return a * b" in diff  # as context/removed line: the new base's text
            assert await workspace.changed_files() == ["src/app.py"]
            squashed = await workspace.squash("Fix add")
            assert git(workspace.path, "rev-parse", "HEAD^") == tip
            assert squashed == await workspace.repo.head_sha()

    async def test_advance_base_refuses_a_commit_head_does_not_contain(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            upstream = push_upstream({"README.md": "# sample\nmore\n"})
            await workspace.target_moved()
            with pytest.raises(RuntimeError, match="not contained"):
                await workspace.advance_base(upstream)
            assert workspace.base_sha != upstream

    async def test_advance_base_requires_a_full_sha(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            with pytest.raises(ValueError, match="full commit sha"):
                await workspace.advance_base("main")

    async def test_delete_modify_is_reported_distinguishably(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            write(workspace.path / "README.md", "# sample\nagent edit\n")
            await workspace.record_attempt("edit readme")
            push_upstream({"README.md": None})
            _, tip = await workspace.target_moved()

            assert (await workspace.repo.merge_tree("HEAD", tip)).conflicted_paths == (b"README.md",)
            assert await workspace.repo.merge_no_commit(tip) is False
            (entry,) = await workspace.repo.unmerged_files()
            assert conflict_kind(entry.stages) == CONFLICT_DELETE_MODIFY

    async def test_both_added_is_reported_distinguishably(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            write(workspace.path / "new.py", "A = 1\n")
            await workspace.record_attempt("add")
            push_upstream({"new.py": "A = 2\n"})
            _, tip = await workspace.target_moved()
            await workspace.repo.merge_no_commit(tip)
            (entry,) = await workspace.repo.unmerged_files()
            assert conflict_kind(entry.stages) == CONFLICT_BOTH_ADDED


class TestCommitResolutionRefusals:
    async def _in_conflict(self, workspace, push_upstream):
        await workspace.start_agent_branch(1, TASK_ID)
        write(workspace.path / "src" / "app.py", APP_AGENT)
        await workspace.record_attempt("fix")
        push_upstream({"src/app.py": APP_UPSTREAM})
        _, tip = await workspace.target_moved()
        await workspace.repo.merge_no_commit(tip)

    @pytest.mark.parametrize("marker", ["<<<<<<< HEAD", "=======", ">>>>>>> theirs", "||||||| base"])
    async def test_a_remaining_marker_is_refused(self, origin_url, push_upstream, marker):
        async with workspace_for(origin_url) as workspace:
            await self._in_conflict(workspace, push_upstream)
            write(workspace.path / "src" / "app.py", f"def add(a, b):\n{marker}\n    return a + b\n")
            with pytest.raises(RuntimeError, match="conflict marker"):
                await workspace.commit_resolution(["src/app.py"], "merge")
            assert await workspace.repo.merge_in_progress()

    async def test_an_unmerged_path_left_out_is_refused(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            await self._in_conflict(workspace, push_upstream)
            write(workspace.path / "src" / "app.py", APP_AGENT)
            with pytest.raises(RuntimeError, match="not unmerged"):
                await workspace.commit_resolution(["README.md"], "merge")

    async def test_an_edit_outside_the_named_paths_is_refused(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            await self._in_conflict(workspace, push_upstream)
            write(workspace.path / "src" / "app.py", APP_AGENT)
            write(workspace.path / "README.md", "# tampered\n")
            with pytest.raises(RuntimeError, match="outside the resolved set"):
                await workspace.commit_resolution(["src/app.py"], "merge")

    @pytest.mark.parametrize("path", [".git/config", ".GIT/hooks/x", "src/../.git/HEAD", "/etc/passwd", "../x"])
    async def test_a_hostile_path_is_refused(self, origin_url, push_upstream, path):
        async with workspace_for(origin_url) as workspace:
            await self._in_conflict(workspace, push_upstream)
            with pytest.raises(Exception, match=r"refus|outside|absolute"):
                await workspace.commit_resolution([path], "merge")

    async def test_a_symlink_is_refused(self, origin_url, push_upstream, tmp_path):
        async with workspace_for(origin_url) as workspace:
            await self._in_conflict(workspace, push_upstream)
            target = tmp_path / "secret.txt"
            target.write_text("secret\n")
            (workspace.path / "link.py").symlink_to(target)
            with pytest.raises(Exception, match="symlink"):
                await workspace.commit_resolution(["link.py"], "merge")

    async def test_no_merge_in_progress_is_refused(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            with pytest.raises(RuntimeError, match="no merge in progress"):
                await workspace.commit_resolution(["README.md"], "merge")

    async def test_an_empty_message_is_refused(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            await self._in_conflict(workspace, push_upstream)
            with pytest.raises(ValueError, match="non-empty"):
                await workspace.commit_resolution(["src/app.py"], "  ")


class TestHostileCheckout:
    """Executable configuration planted in the checkout's own `.git` must not run on the host.

    Two different defences, tested separately. Hook files and the two keys that
    every invocation already overrides on the command line (`core.hooksPath`,
    `core.fsmonitor`) simply do not run. Everything else a checkout's config can
    smuggle in (merge drivers, clean filters, ssh commands, URL rewrites) is
    caught by the closed allowlist in `require_trusted_config`: the operation is
    refused before git is asked to do anything.
    """

    async def test_planted_hook_files_do_not_run_through_detect_merge_and_commit(
        self, origin_url, push_upstream, tmp_path
    ):
        sentinel = tmp_path / "ran"
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            write(workspace.path / "src" / "app.py", APP_AGENT)
            await workspace.record_attempt("fix")
            push_upstream({"src/app.py": APP_UPSTREAM})
            hooks = workspace.path / ".git" / "hooks"
            hooks.mkdir(exist_ok=True)
            for hook in ("post-commit", "post-merge", "pre-merge-commit", "prepare-commit-msg",
                         "commit-msg", "post-checkout", "reference-transaction", "pre-auto-gc"):
                (hooks / hook).write_text(f"#!/bin/sh\ntouch {sentinel}\n")
                (hooks / hook).chmod(0o755)

            _, tip = await workspace.target_moved()
            await workspace.repo.merge_tree("HEAD", tip)
            await workspace.repo.merge_no_commit(tip)
            await workspace.repo.unmerged_files()
            write(workspace.path / "src" / "app.py", APP_AGENT)
            await workspace.commit_resolution(["src/app.py"], "merge")
            await workspace.advance_base(tip)
            await workspace.review_diff()

            assert not sentinel.exists(), "a hook planted in the checkout executed on the host"

    async def test_an_unmodified_clone_passes_the_config_check(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            await workspace.repo.require_trusted_config()

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("core.fsmonitor", "{script}"),
            ("core.hooksPath", "{script}"),
            ("core.editor", "{script}"),
            ("core.pager", "{script}"),
            ("core.sshCommand", "{script}"),
            ("core.attributesFile", "{script}"),
            ("merge.evil.driver", "{script} %O %A %B"),
            ("filter.evil.clean", "{script}"),
            ("filter.evil.smudge", "{script}"),
            ("diff.evil.textconv", "{script}"),
            ("credential.helper", "!{script}"),
            ("url.http://127.0.0.1/.insteadOf", "https://github.com/"),
            ("include.path", "{script}"),
            ("alias.merge", "!{script}"),
            ("extensions.worktreeConfig", "true"),
        ],
    )
    async def test_a_key_nothing_in_our_flow_writes_stops_every_primitive(
        self, origin_url, push_upstream, tmp_path, key, value
    ):
        sentinel = tmp_path / "ran"
        script = tmp_path / "evil.sh"
        script.write_text(f"#!/bin/sh\ntouch {sentinel}\ncat >/dev/null 2>&1\n")
        script.chmod(0o755)
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            write(workspace.path / ".gitattributes", "*.py merge=evil filter=evil diff=evil\n")
            write(workspace.path / "src" / "app.py", APP_AGENT)
            await workspace.record_attempt("fix")
            tip = push_upstream({"src/app.py": APP_UPSTREAM})
            await workspace.target_moved()
            git(workspace.path, "config", key, value.format(script=script))
            repo = workspace.repo

            for call in (
                lambda: repo.fetch_branch("main"),
                lambda: repo.merge_tree("HEAD", tip),
                lambda: repo.merge_no_commit(tip),
                repo.abort_merge,
                lambda: repo.stage_paths(["src/app.py"]),
                lambda: repo.commit_merge("merge"),
            ):
                with pytest.raises(UntrustedConfigError):
                    await call()

            assert not sentinel.exists(), f"{key} from the checkout's config ran on the host"

    async def test_the_guard_is_what_stops_a_merge_driver(self, origin_url, push_upstream, tmp_path):
        """Control: without the allowlist the same setup *does* run the driver on the host,
        so the refusal above is not a vacuous pass."""
        sentinel = tmp_path / "driver-ran"
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            write(workspace.path / ".gitattributes", "*.py merge=evil\n")
            write(workspace.path / "src" / "app.py", APP_AGENT)
            await workspace.record_attempt("fix")
            tip = push_upstream({"src/app.py": APP_UPSTREAM})
            await workspace.target_moved()
            git(workspace.path, "config", "merge.evil.driver", f"sh -c 'touch {sentinel}; false' %O %A %B")

            await workspace.repo._run_checked(  # bypasses the guard on purpose
                "merge-tree", "--write-tree", "HEAD", tip, ok_codes=frozenset({0, 1})
            )

            assert sentinel.exists(), "the control no longer reproduces: strengthen this test"

    async def test_a_clean_filter_is_refused_before_commit_resolution_stages_anything(
        self, origin_url, push_upstream, tmp_path
    ):
        sentinel = tmp_path / "filter-ran"
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            write(workspace.path / "src" / "app.py", APP_AGENT)
            await workspace.record_attempt("fix")
            tip = push_upstream({"src/app.py": APP_UPSTREAM})
            await workspace.target_moved()
            await workspace.repo.merge_no_commit(tip)
            write(workspace.path / ".gitattributes", "*.py filter=evil\n")
            git(workspace.path, "config", "filter.evil.clean", f"sh -c 'touch {sentinel}; cat'")
            write(workspace.path / "src" / "app.py", APP_AGENT)

            with pytest.raises(UntrustedConfigError):
                await workspace.commit_resolution(["src/app.py"], "merge")

            assert not sentinel.exists(), "a clean filter from the checkout's config ran on the host"




class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[str]] = []

    def do_GET(self):
        type(self).seen.append(str(self.headers))
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="x"')
        self.end_headers()

    def log_message(self, *args):
        pass


class TestBranchNames:
    @pytest.mark.parametrize(
        "name", ["--upload-pack=touch x", "-x", "a..b", "a b", "", "@{-1}", "a\nb", "a:b", "x.lock", "a//b", "~x"]
    )
    async def test_an_unsafe_branch_name_is_refused_before_any_fetch(self, origin_url, name):
        async with workspace_for(origin_url) as workspace:
            with pytest.raises(GitError, match="refusing branch name"):
                await workspace.repo.fetch_branch(name)

    async def test_an_option_looking_name_never_reaches_git(self, origin_url, monkeypatch):
        async with workspace_for(origin_url) as workspace:
            calls: list[tuple[str, ...]] = []
            real = repo_module.run_git_checked

            async def spy(*args, **kwargs):
                calls.append(args)
                return await real(*args, **kwargs)

            monkeypatch.setattr(repo_module, "run_git_checked", spy)
            with pytest.raises(GitError):
                await workspace.repo.fetch_branch("--upload-pack=touch x")
            assert calls == []

    async def test_a_missing_branch_fails_loudly(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            with pytest.raises(GitError):
                await workspace.repo.fetch_branch("no-such-branch")

    async def test_a_nested_branch_name_fetches(self, origin_url, source_repo, origin):
        git(source_repo, "branch", "release/1.0", "main")
        git(source_repo, "push", "-q", str(origin), "release/1.0")
        async with workspace_for(origin_url) as workspace:
            assert await workspace.repo.fetch_branch("release/1.0") == workspace.base_sha


class TestCredentialOnFetch:
    async def test_a_rewritten_remote_does_not_receive_the_token(self, origin_url, tmp_path):
        """Verify can rewrite `remote.origin.url`; the helper must release the token
        only for https + the expected host, so a plain-http listener sees no credential."""
        _Recorder.seen = []
        server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            async def provider() -> str:
                return TOKEN

            async with workspace_for(origin_url, token_provider=provider) as workspace:
                git(workspace.path, "config", "remote.origin.url", f"http://127.0.0.1:{server.server_port}/x.git")
                # The URL check would refuse first; lift it so this test still
                # proves the *credential helper* is the second, independent control.
                unchecked = dataclasses.replace(workspace.repo, expected_remote_url=None)
                with pytest.raises(GitError):
                    await unchecked.fetch_branch("main")
        finally:
            server.shutdown()

        assert _Recorder.seen, "the listener was never contacted, so the test proved nothing"
        assert not any(TOKEN in headers or "Authorization" in headers for headers in _Recorder.seen)

    async def test_the_fetch_is_authenticated(self, origin_url):
        """It must ask for a token, or a private remote could never be fetched."""
        asked: list[bool] = []

        async def provider() -> str:
            asked.append(True)
            return TOKEN

        async with workspace_for(origin_url, token_provider=provider) as workspace:
            asked.clear()
            await workspace.target_moved()
            assert asked, "fetch_branch did not request a token"

    def test_the_helper_still_refuses_another_host(self):
        out = subprocess.run(
            ["git", *repo_module._BASE_ARGS, "credential", "fill"],
            input="protocol=https\nhost=evil.example\n\n",
            env=repo_module._git_env(TOKEN, "github.com"),
            capture_output=True, text=True, check=False,
        )
        assert TOKEN not in out.stdout


class TestResolutionScope:
    """`commit_resolution` commits the conflicted paths and nothing else."""

    async def _in_conflict(self, workspace, push_upstream):
        await workspace.start_agent_branch(1, TASK_ID)
        write(workspace.path / "src" / "app.py", APP_AGENT)
        await workspace.record_attempt("fix")
        push_upstream({"src/app.py": APP_UPSTREAM})
        _, tip = await workspace.target_moved()
        await workspace.repo.merge_no_commit(tip)
        head = await workspace.repo.head_sha()
        write(workspace.path / "src" / "app.py", APP_AGENT)
        return head

    async def test_an_extra_modified_tracked_file_in_paths_is_refused(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            head = await self._in_conflict(workspace, push_upstream)
            write(workspace.path / "README.md", "# tampered\n")
            with pytest.raises(RuntimeError, match="not unmerged"):
                await workspace.commit_resolution(["src/app.py", "README.md"], "merge")
            assert await workspace.repo.head_sha() == head
            assert await workspace.repo.merge_in_progress()

    async def test_an_extra_untracked_file_in_paths_is_refused(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            head = await self._in_conflict(workspace, push_upstream)
            write(workspace.path / "stray.py", "X = 1\n")
            with pytest.raises(RuntimeError, match="not unmerged"):
                await workspace.commit_resolution(["src/app.py", "stray.py"], "merge")
            assert await workspace.repo.head_sha() == head

    async def test_a_modified_tracked_file_left_out_of_paths_is_refused(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            head = await self._in_conflict(workspace, push_upstream)
            write(workspace.path / "README.md", "# tampered\n")
            with pytest.raises(RuntimeError, match="outside the resolved set"):
                await workspace.commit_resolution(["src/app.py"], "merge")
            assert await workspace.repo.head_sha() == head

    async def test_an_untracked_file_left_out_of_paths_is_refused(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            head = await self._in_conflict(workspace, push_upstream)
            write(workspace.path / "stray.py", "X = 1\n")
            with pytest.raises(RuntimeError, match="outside the resolved set"):
                await workspace.commit_resolution(["src/app.py"], "merge")
            assert await workspace.repo.head_sha() == head

    async def test_the_upstream_sides_staged_changes_are_not_mistaken_for_strays(
        self, origin_url, push_upstream
    ):
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(1, TASK_ID)
            write(workspace.path / "src" / "app.py", APP_AGENT)
            await workspace.record_attempt("fix")
            push_upstream({"src/app.py": APP_UPSTREAM, "README.md": "# sample\nupstream\n"})
            _, tip = await workspace.target_moved()
            await workspace.repo.merge_no_commit(tip)
            write(workspace.path / "src" / "app.py", APP_AGENT)

            sha = await workspace.commit_resolution(["src/app.py"], "merge")
            assert sha == await workspace.repo.head_sha()

    async def test_more_paths_than_the_cap_are_refused(self, origin_url, push_upstream):
        async with workspace_for(origin_url) as workspace:
            await self._in_conflict(workspace, push_upstream)
            many = [f"f{i}.py" for i in range(MAX_RESOLUTION_PATHS + 1)]
            with pytest.raises(ValueError, match="at most 500"):
                await workspace.commit_resolution(many, "merge")

    async def test_more_unmerged_paths_than_the_cap_are_refused(self, origin_url, monkeypatch):
        async with workspace_for(origin_url) as workspace:
            records = b"".join(
                b"100644 " + b"1" * 40 + b" 2\tf" + str(i).encode() + b"\x00"
                for i in range(MAX_UNMERGED_PATHS + 1)
            )

            async def fake(*args, **kwargs):
                return records

            monkeypatch.setattr(repo_module, "run_git_bytes", fake)
            with pytest.raises(GitError, match="refusing more than 500"):
                await workspace.repo.unmerged_files()


class TestConfigValues:
    """Allowed keys must also hold the values our flow wrote."""

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("remote.origin.url", "https://evil.example/x.git"),
            ("remote.origin.fetch", "+refs/*:refs/*"),
            ("branch.main.remote", "elsewhere"),
            ("branch.main.merge", "refs/tags/x"),
        ],
    )
    async def test_a_changed_value_is_refused(self, origin_url, key, value):
        async with workspace_for(origin_url) as workspace:
            git(workspace.path, "config", key, value)
            with pytest.raises(UntrustedConfigError, match="unexpected value"):
                await workspace.repo.require_trusted_config()
            with pytest.raises(UntrustedConfigError):
                await workspace.repo.fetch_branch("main")

    async def test_the_values_the_clone_wrote_are_accepted(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            assert workspace.repo.expected_remote_url == origin_url
            await workspace.repo.require_trusted_config()


class TestFetchIntegrity:
    async def test_the_fetch_asks_git_to_check_objects(self, origin_url, monkeypatch):
        async with workspace_for(origin_url) as workspace:
            calls: list[tuple[str, ...]] = []
            real = repo_module.run_git

            async def spy(*args, **kwargs):
                calls.append(args)
                return await real(*args, **kwargs)

            monkeypatch.setattr(repo_module, "run_git", spy)
            await workspace.repo.fetch_branch("main")
            (fetch,) = [c for c in calls if "fetch" in c]
            assert "transfer.fsckObjects=true" in fetch
