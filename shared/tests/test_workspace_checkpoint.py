"""Capturing an edited tree as two patches and rebuilding it on a fresh clone, against real git.

The clone is deleted with the process, so this is the whole of what a checkpoint keeps of the work: the
tree must come back byte for byte, including a new file, a binary file and a mode change, and the scored
tree must be a commit the later work sits on.
"""

import os
from pathlib import Path

import pytest
from repolace_shared.git.workspace import task_workspace
from shared_support import git, write

pytestmark = pytest.mark.anyio


def workspace_for(origin_url: str):
    return task_workspace("acme", "sample", target_branch="main", clone_url=origin_url)


def tree_of(workspace) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for root, dirs, files in os.walk(workspace.path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            full = os.path.join(root, name)
            out[os.path.relpath(full, workspace.path)] = Path(full).read_bytes()
    return out


async def edit_in_two_steps(workspace) -> str:
    """One scored edit, then more work on top of it that was never scored. Returns the scored sha."""
    write(workspace.path / "src" / "app.py", "VALUE = 'scored'\n")
    await workspace.record_attempt("attempt 1")
    scored = await workspace.repo.head_sha()
    write(workspace.path / "src" / "app.py", "VALUE = 'scored'\nEXTRA = 1\n")
    write(workspace.path / "src" / "new_module.py", "NEW = True\n")
    (workspace.path / "blob.bin").write_bytes(bytes(range(256)))
    return scored


class TestCaptureAndRestore:
    async def test_a_restored_clone_has_the_same_tree(self, origin_url):
        async with workspace_for(origin_url) as first:
            scored = await edit_in_two_steps(first)
            scored_patch, delta_patch = await first.capture(scored)
            expected = tree_of(first)

        assert scored_patch and delta_patch
        async with workspace_for(origin_url) as second:
            assert second.base_sha == first.base_sha
            await second.restore(scored_patch, delta_patch)
            assert tree_of(second) == expected

    async def test_the_scored_tree_is_a_commit_and_the_work_since_sits_on_it(self, origin_url):
        async with workspace_for(origin_url) as first:
            scored = await edit_in_two_steps(first)
            scored_patch, delta_patch = await first.capture(scored)

        async with workspace_for(origin_url) as second:
            restored_scored = await second.restore(scored_patch, delta_patch)
            assert restored_scored is not None
            # The scored commit holds the scored content and none of the later work.
            assert git(second.path, "show", f"{restored_scored}:src/app.py") == "VALUE = 'scored'"
            assert git(second.path, "cat-file", "-t", restored_scored) == "commit"
            # HEAD is a descendant, and the change since the scored commit is exactly the later work.
            changed = await second.repo.changed_files_from(restored_scored)
            assert sorted(changed) == ["blob.bin", "src/app.py", "src/new_module.py"]
            assert await second.repo.head_sha() != restored_scored

    async def test_with_nothing_scored_there_is_only_the_delta(self, origin_url):
        async with workspace_for(origin_url) as first:
            write(first.path / "src" / "app.py", "VALUE = 'wip'\n")
            scored_patch, delta_patch = await first.capture(None)

        assert scored_patch == b"" and delta_patch
        async with workspace_for(origin_url) as second:
            assert await second.restore(scored_patch, delta_patch) is None
            assert (second.path / "src" / "app.py").read_text() == "VALUE = 'wip'\n"

    async def test_a_clean_tree_captures_nothing_and_restores_to_the_base(self, origin_url):
        async with workspace_for(origin_url) as first:
            scored_patch, delta_patch = await first.capture(None)

        assert scored_patch == b"" and delta_patch == b""
        async with workspace_for(origin_url) as second:
            assert await second.restore(scored_patch, delta_patch) is None
            assert await second.repo.head_sha() == second.base_sha

    async def test_capture_does_not_change_the_work_it_records(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            scored = await edit_in_two_steps(workspace)
            before = tree_of(workspace)
            await workspace.capture(scored)
            await workspace.capture(scored)  # twice: the second finds a clean tree
            assert tree_of(workspace) == before

    async def test_a_patch_that_does_not_apply_fails_loudly(self, origin_url):
        async with workspace_for(origin_url) as workspace:
            with pytest.raises(Exception, match="apply"):
                await workspace.restore(b"", b"not a patch at all\n")
