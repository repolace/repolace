"""Unit tests for the per-task checkout lifecycle.

The interesting assertions are the ones about the seams between steps: that the
base commit is captured before any edit, that the branch actually reaches the
remote before the directory is discarded, and that discarding it can never
swallow the reason a task failed.
"""

import os
import shutil
import stat
import uuid
from pathlib import Path

import pytest
import structlog

from repolace_shared.git.repo import GitExportError
from repolace_shared.git.workspace import (
    agent_branch_name,
    github_clone_url,
    task_workspace,
)

from shared_support import git, git_bytes, git_check_ref, write

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


class TestExportTree:
    """The `.git`-less export is the confused-deputy mitigation.

    Its value is entirely in what it leaves *out*, so these tests are mostly
    assertions of absence -- which is the kind of test that quietly stops
    meaning anything if the export silently starts producing nothing at all.
    Hence the positive assertions alongside each one.
    """

    async def test_the_source_is_exported(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            export = await workspace.export_tree(0)

            assert (export / "src" / "app.py").is_file()
            assert (export / "README.md").is_file()

    async def test_git_is_not_exported(self, origin_url):
        """The whole point: no .git means no hooks to plant and no config to poison."""
        async with workspace_for(origin_url) as workspace:
            export = await workspace.export_tree(0)

            assert not (export / ".git").exists()
            assert list(export.rglob(".git")) == []

    async def test_untracked_files_are_left_behind(self, origin_url):
        """A test run's artifacts must not become the next attempt's input."""
        async with workspace_for(origin_url) as workspace:
            write(workspace.path / "junk.log", "debug output\n")
            write(workspace.path / ".pytest_cache" / "lastfailed", "{}\n")

            export = await workspace.export_tree(0)

            assert not (export / "junk.log").exists()
            assert not (export / ".pytest_cache").exists()

    async def test_ignored_build_artifacts_are_left_behind(self, origin_url):
        """`reset_hard` cleans with -fd, not -fdx, so ignored artifacts survive
        in the checkout between attempts. Exporting from the index drops them."""
        async with workspace_for(origin_url) as workspace:
            write(workspace.path / ".gitignore", "*.so\n")
            write(workspace.path / "stale.so", "compiled last attempt\n")

            export = await workspace.export_tree(0)

            assert not (export / "stale.so").exists()
            assert (export / "src" / "app.py").is_file()

    async def test_committed_changes_are_exported(self, origin_url):
        """The export must reflect the attempt, or the results describe the wrong code."""
        async with workspace_for(origin_url) as workspace:
            await workspace.start_agent_branch(ISSUE_NUMBER, TASK_ID)
            write(workspace.path / "src" / "app.py", "def add(a, b):\n    return a + b\n")
            await workspace.record_attempt("fix")

            export = await workspace.export_tree(1)

            assert "return a + b" in (export / "src" / "app.py").read_text()

    async def test_a_staged_edit_is_refused(self, origin_url):
        """Exporting while the index disagrees with HEAD would attribute results
        to a commit that never held that code."""
        async with workspace_for(origin_url) as workspace:
            write(workspace.path / "src" / "app.py", "def add(a, b):\n    return 0\n")
            await workspace.repo._run("add", "--all")

            with pytest.raises(RuntimeError, match="does not match HEAD"):
                await workspace.export_tree(0)

    async def test_an_unstaged_edit_is_refused(self, origin_url):
        """The finding. The guard passed `diff-index --cached`, which compares
        the index to HEAD and ignores the working tree -- and the edit stage
        writes the working tree directly, with staging happening later in
        `record_attempt`. So an unstaged edit exported HEAD's content, the
        sandbox tested code the agent had not written, and the pass/fail sets
        were attributed to the attempt anyway. A wrong measurement, not an
        error."""
        async with workspace_for(origin_url) as workspace:
            write(workspace.path / "src" / "app.py", "def add(a, b):\n    return 0\n")

            with pytest.raises(RuntimeError, match="does not match HEAD"):
                await workspace.export_tree(0)

    async def test_untracked_test_artifacts_do_not_block_the_export(self, origin_url):
        """A test run leaves .pytest_cache and __pycache__ behind, and neither
        is a disagreement with HEAD. `-uno` is what keeps a second attempt from
        being refused for the first attempt's litter."""
        async with workspace_for(origin_url) as workspace:
            write(workspace.path / ".pytest_cache" / "v" / "lastfailed", "{}\n")
            write(workspace.path / "src" / "__pycache__" / "app.pyc", "junk\n")

            export = await workspace.export_tree(0)

            assert (export / "src" / "app.py").is_file()

    async def test_the_exported_bytes_are_the_committed_bytes(self, origin_url):
        """The whole claim of the rewrite, and it fails under `checkout-index`.

        The `hazards` branch ships `.gitattributes` with `*.py text eol=crlf`.
        checkout-index honours that, so the sandbox ran CRLF while the commit
        and the PR diff showed LF -- what was tested was not what was reviewed.
        Reading blobs consults no attribute machinery at all.
        """
        async with workspace_for(origin_url, branch="hazards") as workspace:
            export = await workspace.export_tree(0)

            content = (export / "src" / "app.py").read_bytes()
            assert b"\r\n" not in content
            assert content == b"def add(a, b):\n    return a - b\n"

    async def test_a_repo_local_smudge_filter_does_not_run_during_export(self, origin_url, tmp_path):
        """Pinning git's config files closed the operator-config half of this.
        It does not close the checkout's own .git/config -- which is precisely
        what the sandbox can write. Reading the object store closes both, which
        is why the rewrite is structural rather than another key to clear."""
        sentinel = tmp_path / "smudge-ran"
        async with workspace_for(origin_url, branch="hazards") as workspace:
            await workspace.repo._run(
                "config", "filter.evil.smudge", f'sh -c "echo owned > {sentinel}; cat"'
            )
            await workspace.repo._run("config", "filter.evil.required", "false")
            write(workspace.path / ".gitattributes", "*.py filter=evil\n")
            await workspace.repo._run("add", "--all")
            await workspace.repo.commit_all("use the filter")

            export = await workspace.export_tree(0)
            blob = git_bytes(workspace.path, "cat-file", "blob", "HEAD:src/app.py")

            assert not sentinel.exists(), "a filter from the checkout's own config ran on the host"
            # Against the blob, not a literal: this branch also carries
            # `text eol=crlf`, and replacing .gitattributes to install the
            # filter drops that rule, so what the blob holds depends on history.
            # The invariant under test is fidelity to the object store, whatever
            # it happens to contain.
            assert (export / "src" / "app.py").read_bytes() == blob

    async def test_the_executable_bit_survives_the_export(self, origin_url):
        """Suites that shell out to a tracked script need it, and the mode is
        now set by us rather than inherited from a checkout."""
        async with workspace_for(origin_url, branch="hazards") as workspace:
            export = await workspace.export_tree(0)

            assert os.access(export / "bin" / "run.sh", os.X_OK)
            assert not os.access(export / "src" / "app.py", os.X_OK)
            assert stat.S_IMODE((export / "src" / "app.py").stat().st_mode) == 0o644

    async def test_the_sandbox_can_write_into_a_subdirectory_of_the_export(self, origin_url):
        """The regression test for the mode bug. Only the export *root* was ever
        chmodded, and checkout-index created subdirectories at 0755 owned by the
        worker -- so a sandbox running as an unprivileged uid got EACCES writing
        a __pycache__ beside its own code, which surfaces as an unscoreable run
        and silently drops the instance from the benchmark."""
        async with workspace_for(origin_url, branch="hazards") as workspace:
            export = await workspace.export_tree(0)

            nested = export / "deep" / "nested"
            assert stat.S_IMODE(nested.stat().st_mode) == 0o777
            assert stat.S_IMODE((export / "deep").stat().st_mode) == 0o777
            (nested / "__pycache__").mkdir()

    async def test_a_committed_symlink_is_refused(self, origin, origin_url, repo_with_symlink, tmp_path):
        """A symlink is not a file with content, so "the bytes match the commit"
        stops being a well-formed claim -- and materialising one puts a path
        resolving outside the export into a tree the host later deletes."""
        source = repo_with_symlink()
        # `origin` was cloned from `source_repo` at fixture setup, before this
        # body ran, so the new commit has to be pushed for the clone to see it.
        git(source, "push", "--force", str(origin), "hazards")
        victim = tmp_path / "victim"
        victim.write_text("UNTOUCHED\n")

        async with workspace_for(origin_url, branch="hazards") as workspace:
            with pytest.raises(GitExportError, match="link.py"):
                await workspace.export_tree(0)

        assert victim.read_text() == "UNTOUCHED\n"

    async def test_a_submodule_is_refused(self, origin, origin_url, source_repo):
        """checkout-index left an empty directory, so the suite ran against
        silently missing sources -- a confidently wrong number, not an error."""
        git(source_repo, "checkout", "hazards")
        # A real commit sha: git refuses an all-zero one, and any commit object
        # will do -- nothing resolves the gitlink, it only has to be present.
        sha = git(source_repo, "rev-parse", "HEAD")
        git(source_repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/lib")
        git(source_repo, "commit", "-m", "add gitlink")
        git(source_repo, "push", "--force", str(origin), "hazards")
        git(source_repo, "checkout", "main")

        async with workspace_for(origin_url, branch="hazards") as workspace:
            with pytest.raises(GitExportError, match="160000"):
                await workspace.export_tree(0)

    async def test_a_non_utf8_path_round_trips(self, origin, origin_url, source_repo):
        """What keeping paths as bytes all the way to the filesystem buys. A
        repository is entitled to a filename that is not valid UTF-8, and
        decoding one with errors="replace" would write a *different* file."""
        git(source_repo, "checkout", "hazards")
        name = b"src/caf\xe9.py"
        oid = git(source_repo, "hash-object", "-w", "--stdin", input_text="VALUE = 3\n")
        git(source_repo, "update-index", "--add", "--cacheinfo",
            f"100644,{oid},{os.fsdecode(name)}")
        git(source_repo, "commit", "-m", "latin-1 filename")
        git(source_repo, "push", "--force", str(origin), "hazards")
        git(source_repo, "checkout", "main")

        async with workspace_for(origin_url, branch="hazards") as workspace:
            export = await workspace.export_tree(0)

            assert (export / os.fsdecode(name)).read_bytes() == b"VALUE = 3\n"

    async def test_each_attempt_gets_its_own_directory(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            first = await workspace.export_tree(0)
            second = await workspace.export_tree(1)

            assert first != second
            assert first.is_dir() and second.is_dir()

    async def test_the_sandbox_can_write_into_the_export(self, origin_url):
        """It runs as a different uid; without this it cannot even create a pycache."""
        async with workspace_for(origin_url) as workspace:
            export = await workspace.export_tree(0)

            assert stat.S_IMODE(export.stat().st_mode) == 0o777

    async def test_exports_are_cleaned_up_with_the_workspace(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            root = workspace.root
            export = await workspace.export_tree(0)
            results = await workspace.results_dir(0)
            assert export.exists() and results.exists()

        assert not root.exists()


class TestLabelledRuns:
    """A str label is an unscored run (a probe, a scratch script): its own directory
    beside the attempts', so it can never collide with one."""

    async def test_an_int_attempt_keeps_the_directory_names_it_always_had(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            export = await workspace.export_tree(3)
            results = await workspace.results_dir(3)

            assert export == workspace.root / "export-3"
            assert results == workspace.root / "results-3"

    async def test_a_label_gets_its_own_export_and_results_directory(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            export = await workspace.export_tree("probe-1")
            results = await workspace.results_dir("probe-1")

            assert export == workspace.root / "export-probe-1"
            assert results == workspace.root / "results-probe-1"
            assert (export / "src" / "app.py").exists()

    async def test_a_label_directory_is_distinct_from_every_attempt_and_label(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            exports = {
                await workspace.export_tree(0),
                await workspace.export_tree(1),
                await workspace.export_tree("probe-1"),
                await workspace.export_tree("probe-2"),
                await workspace.export_tree("script-1"),
            }

            assert len(exports) == 5

    async def test_the_export_is_the_same_bytes_whatever_the_key(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            by_int = await workspace.export_tree(0)
            by_label = await workspace.export_tree("probe-1")

            for relative in ("README.md", "src/app.py"):
                assert (by_int / relative).read_bytes() == (by_label / relative).read_bytes()

    async def test_the_results_directory_is_writable_by_the_sandbox_uid(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            results = await workspace.results_dir("script-1")

            assert stat.S_IMODE(results.stat().st_mode) == 0o777

    async def test_a_dirty_tree_is_refused_for_a_label_as_for_an_attempt(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            (workspace.path / "src" / "app.py").write_text("edited\n")

            with pytest.raises(RuntimeError, match="refusing to export"):
                await workspace.export_tree("probe-1")

    @pytest.mark.parametrize(
        "label",
        ["", "1", "42", "-x", ".hidden", "probe/3", "../x", "x/..", "probe 3", "probe:3", "a\x00b", "p" + "a" * 64],
    )
    async def test_a_label_that_could_name_anything_else_is_refused(self, origin_url, label):
        """`discard` deletes the tree under the name, so `x/..` must never be able to
        mean the workspace root, and `"1"` must not alias the int attempt 1."""
        async with workspace_for(origin_url) as workspace:
            with pytest.raises(ValueError, match="run label"):
                await workspace.export_tree(label)
            with pytest.raises(ValueError, match="run label"):
                await workspace.results_dir(label)

            assert sorted(p.name for p in workspace.root.iterdir()) == ["repo"]

    async def test_a_label_may_be_exactly_64_characters(self, origin_url):
        label = "p" + "a" * 63
        async with workspace_for(origin_url) as workspace:
            assert (await workspace.export_tree(label)).name == f"export-{label}"


class TestDiscard:
    async def test_it_removes_that_labels_export_and_results(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            export = await workspace.export_tree("probe-1")
            results = await workspace.results_dir("probe-1")
            (results / "report.jsonl").write_text("{}\n")

            await workspace.discard("probe-1")

            assert not export.exists() and not results.exists()

    async def test_it_leaves_every_other_run_and_the_checkout_alone(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            baseline = await workspace.export_tree(0)
            other = await workspace.export_tree("probe-2")
            await workspace.results_dir(0)
            await workspace.export_tree("probe-1")

            await workspace.discard("probe-1")

            assert baseline.is_dir() and other.is_dir()
            assert (workspace.root / "results-0").is_dir()
            assert (workspace.path / "src" / "app.py").exists()

    async def test_a_discarded_label_is_exportable_again_from_nothing(self, origin_url):
        """A stale file in one run's directory must not be visible to the next
        run that gets that directory."""
        async with workspace_for(origin_url) as workspace:
            export = await workspace.export_tree("probe-1")
            (export / "stale.txt").write_text("left by the first run")

            await workspace.discard("probe-1")
            again = await workspace.export_tree("probe-1")

            assert not (again / "stale.txt").exists()

    async def test_it_can_be_repeated_and_can_name_a_label_that_never_ran(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            await workspace.export_tree("probe-1")

            await workspace.discard("probe-1")
            await workspace.discard("probe-1")
            await workspace.discard("script-9")

    async def test_a_label_that_never_ran_is_not_logged_as_a_failure(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            with structlog.testing.capture_logs() as logs:
                await workspace.discard("script-9")

            assert not [entry for entry in logs if "failed" in entry["event"]]

    async def test_it_works_on_an_int_attempt_too(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            export = await workspace.export_tree(2)

            await workspace.discard(2)

            assert not export.exists()

    async def test_a_label_that_could_climb_out_deletes_nothing_and_does_not_raise(self, origin_url):
        """`x/..` is `export-x/..` -- the workspace root -- if it were ever joined
        unvalidated and `export-x` existed. It must be refused, and quietly: a
        discard runs in a `finally`, and a raise there would mask the real error."""
        async with workspace_for(origin_url) as workspace:
            (workspace.root / "export-x").mkdir()

            await workspace.discard("x/..")
            await workspace.discard("../repo")
            await workspace.discard("1")

            assert workspace.path.is_dir() and (workspace.root / "export-x").is_dir()

    async def test_it_does_not_raise_when_a_removal_fails(self, origin_url, monkeypatch):
        async with workspace_for(origin_url) as workspace:
            await workspace.export_tree("probe-1")

            def explode(*args, **kwargs):
                raise OSError("device busy")

            monkeypatch.setattr(shutil, "rmtree", explode)

            await workspace.discard("probe-1")  # logged, never raised

            monkeypatch.undo()

    async def test_it_removes_a_directory_the_sandbox_left_unwritable(self, origin_url):
        """Same hazard as the workspace cleanup: the sandbox's own uid created a
        directory the host cannot write into, so a plain rmtree stops at it."""
        async with workspace_for(origin_url) as workspace:
            results = await workspace.results_dir("probe-1")
            locked = results / "locked"
            (locked / "inner").mkdir(parents=True)
            (locked / "inner" / "f").write_text("x")
            locked.chmod(0o500)

            try:
                await workspace.discard("probe-1")
            finally:
                if locked.exists():
                    locked.chmod(0o700)

            assert not results.exists()

    async def test_a_symlink_inside_a_run_is_not_followed(self, origin_url, tmp_path):
        """The sandbox can write into its own results directory and could plant a
        link; removal deletes the link, never the file it names."""
        victim = tmp_path / "victim-dir"
        victim.mkdir()
        (victim / "precious.txt").write_text("keep me")
        async with workspace_for(origin_url) as workspace:
            results = await workspace.results_dir("probe-1")
            (results / "link").symlink_to(victim)

            await workspace.discard("probe-1")

            assert not results.exists()
            assert (victim / "precious.txt").read_text() == "keep me"

    async def test_a_symlink_in_place_of_the_run_directory_is_never_followed(self, origin_url, tmp_path):
        victim = tmp_path / "victim-dir"
        victim.mkdir()
        (victim / "precious.txt").write_text("keep me")
        async with workspace_for(origin_url) as workspace:
            (workspace.root / "export-probe-1").symlink_to(victim)

            await workspace.discard("probe-1")

            assert (victim / "precious.txt").read_text() == "keep me"


class TestPruneRemoteRefs:
    """A full clone carries every branch the remote has. Removing the names is one
    layer against an agent reaching an earlier run's patch -- not the control."""

    @staticmethod
    def remote_refs(workspace) -> set[str]:
        out = git(workspace.path, "for-each-ref", "--format=%(refname)", "refs/remotes")
        return set(out.splitlines())

    async def test_the_clone_starts_with_every_remote_branch(self, origin_url):
        """The premise: without this the tests below would pass vacuously."""
        async with workspace_for(origin_url) as workspace:
            assert {
                "refs/remotes/origin/main",
                "refs/remotes/origin/develop",
                "refs/remotes/origin/hazards",
                "refs/remotes/origin/HEAD",
            } <= self.remote_refs(workspace)

    async def test_only_the_kept_branches_survive(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            removed = await workspace.prune_remote_refs(["main"])

            assert self.remote_refs(workspace) == {"refs/remotes/origin/main"}
            assert set(removed) == {"develop", "hazards"}

    async def test_origin_head_goes_too_so_it_cannot_dangle(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            await workspace.prune_remote_refs(["develop"])

            assert "refs/remotes/origin/HEAD" not in self.remote_refs(workspace)
            assert self.remote_refs(workspace) == {"refs/remotes/origin/develop"}

    async def test_head_is_not_reported_as_a_removed_branch(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            removed = await workspace.prune_remote_refs(["main"])

            assert "HEAD" not in removed

    async def test_several_branches_can_be_kept_including_nested_names(self, origin_url, source_repo, origin):
        git(source_repo, "push", str(origin), "main:refs/heads/bench/x")
        async with workspace_for(origin_url) as workspace:
            await workspace.prune_remote_refs(["main", "bench/x"])

            assert self.remote_refs(workspace) == {"refs/remotes/origin/main", "refs/remotes/origin/bench/x"}

    async def test_a_name_is_compared_exactly_not_as_a_prefix(self, origin_url, source_repo, origin):
        git(source_repo, "push", str(origin), "main:refs/heads/bench/x")
        async with workspace_for(origin_url) as workspace:
            removed = await workspace.prune_remote_refs(["main", "bench"])

            assert "bench/x" in removed
            assert self.remote_refs(workspace) == {"refs/remotes/origin/main"}

    async def test_keeping_a_branch_that_does_not_exist_is_harmless(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            await workspace.prune_remote_refs(["main", "no-such-branch"])

            assert self.remote_refs(workspace) == {"refs/remotes/origin/main"}

    async def test_keeping_nothing_removes_every_remote_branch(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            await workspace.prune_remote_refs([])

            assert self.remote_refs(workspace) == set()

    async def test_a_second_call_has_nothing_left_to_remove(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            await workspace.prune_remote_refs(["main"])

            assert await workspace.prune_remote_refs(["main"]) == ()

    async def test_a_branch_whose_name_looks_like_an_option_is_removed_not_obeyed(self, origin_url, source_repo, origin):
        """Full ref names go to git, so `--force` or `-x` as a branch component is
        data. A remote chooses these names."""
        git(source_repo, "push", str(origin), "main:refs/heads/--force")
        async with workspace_for(origin_url) as workspace:
            assert "refs/remotes/origin/--force" in self.remote_refs(workspace)

            removed = await workspace.prune_remote_refs(["main"])

            assert "--force" in removed
            assert self.remote_refs(workspace) == {"refs/remotes/origin/main"}

    async def test_the_checkout_itself_is_untouched(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            before = (await workspace.repo.head_sha(), await workspace.repo.current_branch())

            await workspace.prune_remote_refs(["main"])

            assert (await workspace.repo.head_sha(), await workspace.repo.current_branch()) == before
            assert not await workspace.repo.has_changes()
            assert (workspace.path / "src" / "app.py").exists()

    async def test_the_workspace_still_works_afterwards(self, origin_url):
        """Everything downstream resolves shas, not remote names -- the freshness
        diff, the export, the squash and the push."""
        async with workspace_for(origin_url) as workspace:
            await workspace.prune_remote_refs(["main"])
            await workspace.start_agent_branch(ISSUE_NUMBER, TASK_ID)
            (workspace.path / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
            await workspace.record_attempt("attempt 1")

            export = await workspace.export_tree(1)
            await workspace.squash("fix")
            pushed = await workspace.push()

            assert (export / "src" / "app.py").read_text() == "def add(a, b):\n    return a + b\n"
            assert pushed == agent_branch_name(ISSUE_NUMBER, TASK_ID)

    async def test_it_removes_names_not_objects(self, origin_url, source_repo):
        """The honest limit: a sha that is already known still resolves, which is why
        this is defence in depth and not the control."""
        develop_tip = git(source_repo, "rev-parse", "develop")
        async with workspace_for(origin_url) as workspace:
            await workspace.prune_remote_refs(["main"])

            assert git(workspace.path, "cat-file", "-t", develop_tip) == "commit"
            with pytest.raises(Exception):
                git(workspace.path, "rev-parse", "--verify", "--quiet", "origin/develop")
