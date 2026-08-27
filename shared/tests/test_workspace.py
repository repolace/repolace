"""Unit tests for the per-task checkout lifecycle.

The interesting assertions are the ones about the seams between steps: that the
base commit is captured before any edit, that the branch actually reaches the
remote before the directory is discarded, and that discarding it can never
swallow the reason a task failed.
"""

import shutil
import stat
import uuid
from pathlib import Path

import pytest

from repolace_shared.git.workspace import (
    agent_branch_name,
    github_clone_url,
    task_workspace,
)

from support import git, git_check_ref, write

pytestmark = pytest.mark.anyio

TASK_ID = uuid.UUID("2f8a1c4e-0000-4000-8000-000000000001")
ISSUE_NUMBER = 42


def workspace_for(origin_url: str, branch: str = "main"):
    return task_workspace("acme", "sample", target_branch=branch, clone_url=origin_url)


class TestLifecycle:
    async def test_the_checkout_sits_inside_the_workspace_root(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            assert workspace.path.parent == workspace.root
            assert (workspace.path / "src" / "app.py").exists()

    async def test_base_sha_is_the_target_branch_tip(self, origin_url, source_repo):
        async with workspace_for(origin_url) as workspace:
            assert workspace.base_sha == git(source_repo, "rev-parse", "main")

    async def test_a_non_default_target_branch_is_honoured(self, origin_url, source_repo):
        async with workspace_for(origin_url, branch="develop") as workspace:
            assert workspace.target_branch == "develop"
            assert workspace.base_sha == git(source_repo, "rev-parse", "develop")

    async def test_the_workspace_is_deleted_on_exit(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            root = workspace.root
            assert root.exists()

        assert not root.exists()

    async def test_the_workspace_is_deleted_even_when_the_task_fails(self, origin_url):
        root = None
        with pytest.raises(RuntimeError, match="edit stage blew up"):
            async with workspace_for(origin_url) as workspace:
                root = workspace.root
                raise RuntimeError("edit stage blew up")

        assert root is not None and not root.exists()

    async def test_a_cleanup_failure_does_not_mask_the_task_error(self, origin_url, tmp_path, monkeypatch):
        """Losing a temp directory is a disk leak. Losing the reason a task failed is worse."""

        def explode(*args, **kwargs):
            raise OSError("device busy")

        monkeypatch.setattr(shutil, "rmtree", explode)

        # Anchored under tmp_path because cleanup is what we just broke -- this
        # workspace really does survive the test, and pytest sweeps tmp_path.
        with pytest.raises(RuntimeError, match="edit stage blew up"):
            async with task_workspace(
                "acme", "sample", target_branch="main", clone_url=origin_url, parent_dir=tmp_path
            ):
                raise RuntimeError("edit stage blew up")

    async def test_an_unwritable_directory_does_not_block_cleanup(self, origin_url):
        """A read-only *directory* is the case that actually fails.

        On POSIX the permission to unlink comes from the containing directory,
        not the entry -- so a read-only file (what git leaves for pack objects)
        never needed recovering, and testing one proves nothing.
        """
        async with workspace_for(origin_url) as workspace:
            root = workspace.root
            locked = workspace.path / "locked"
            write(locked / "trapped.txt", "cannot reach this\n")
            locked.chmod(0o500)

        assert not root.exists()

    async def test_cleanup_does_not_chmod_through_a_symlink(self, origin_url, tmp_path):
        """`rmtree` is fd-based and symlink-safe; a path-based chmod in the hook gives that back."""
        outsider = tmp_path / "host-file"
        outsider.write_text("not ours\n")
        outsider.chmod(0o644)

        async with workspace_for(origin_url) as workspace:
            locked = workspace.path / "locked"
            locked.mkdir()
            (locked / "link").symlink_to(outsider)
            locked.chmod(0o500)

        assert outsider.exists(), "the symlink target itself must survive"
        assert stat.S_IMODE(outsider.stat().st_mode) == 0o644, "chmod followed the symlink"


class TestBranchNaming:
    def test_the_name_carries_the_issue_and_the_whole_task_id(self):
        name = agent_branch_name(ISSUE_NUMBER, TASK_ID)

        assert name == f"repolace/issue-42-{TASK_ID.hex}"

    def test_ids_sharing_a_long_prefix_still_produce_distinct_branches(self):
        """A prefix of the id would stake uniqueness on 32 bits being random, which uuid7 is not."""
        other = uuid.UUID("2f8a1c4e-0000-4000-8000-000000000002")

        assert agent_branch_name(ISSUE_NUMBER, TASK_ID) != agent_branch_name(ISSUE_NUMBER, other)

    def test_the_name_is_a_valid_git_ref(self):
        assert git_check_ref(agent_branch_name(ISSUE_NUMBER, TASK_ID))


class TestCloneUrl:
    def test_the_derived_remote_is_plain_https(self):
        assert github_clone_url("acme", "sample") == "https://github.com/acme/sample.git"


class TestAgentFlow:
    async def test_attempts_are_squashed_and_pushed_to_the_remote(self, origin_url, origin):
        """The whole recorded flow, end to end, against a real remote."""
        async with workspace_for(origin_url) as workspace:
            base = workspace.base_sha
            branch = await workspace.start_agent_branch(ISSUE_NUMBER, TASK_ID)

            for attempt in range(2):
                write(workspace.path / "src" / "app.py", f"def add(a, b):\n    return a + b  # {attempt}\n")
                assert await workspace.record_attempt(f"attempt {attempt}") is not None

            assert await workspace.squash("Fix add") is not None
            assert await workspace.push() == branch

        # The clone is gone; the branch has to have outlived it.
        assert git(origin, "rev-parse", "--verify", f"refs/heads/{branch}")
        assert git(origin, "rev-list", "--count", f"{base}..{branch}") == "1"
        assert "# 1" in git(origin, "show", f"{branch}:src/app.py")

    async def test_review_diff_matches_what_the_pr_will_show(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(ISSUE_NUMBER, TASK_ID)
            write(workspace.path / "src" / "app.py", "def add(a, b):\n    return a + b\n")
            await workspace.record_attempt("fix")

            diff = await workspace.review_diff()

            assert "+    return a + b" in diff
            assert "-    return a - b" in diff

    async def test_changed_files_expose_test_edits(self, origin_url):
        """Feeds the no-test-edits success criterion, which is why it must see the whole branch."""
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(ISSUE_NUMBER, TASK_ID)
            write(workspace.path / "src" / "app.py", "def add(a, b):\n    return a + b\n")
            await workspace.record_attempt("fix")
            write(workspace.path / "tests" / "test_app.py", "def test_add():\n    assert True\n")
            await workspace.record_attempt("weaken the test")

            assert sorted(await workspace.changed_files()) == ["src/app.py", "tests/test_app.py"]

    async def test_rewind_returns_to_an_earlier_attempt(self, origin_url):
        """How the Debugger abandons a bad attempt instead of trying to un-edit it."""
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(ISSUE_NUMBER, TASK_ID)
            write(workspace.path / "src" / "app.py", "def add(a, b):\n    return a + b\n")
            good = await workspace.record_attempt("good attempt")
            write(workspace.path / "src" / "app.py", "def add(a, b):\n    return ???\n")

            await workspace.rewind_to(good)

            assert (workspace.path / "src" / "app.py").read_text() == "def add(a, b):\n    return a + b\n"

    async def test_pushing_without_a_branch_is_a_programming_error(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            with pytest.raises(RuntimeError, match="no agent branch"):
                await workspace.push()
