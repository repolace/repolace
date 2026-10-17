import os
import time

from repolace_worker.orphans import remove_stale_workspaces


def test_only_old_task_directories_are_removed(tmp_path):
    old = tmp_path / "repolace-task-old"
    fresh = tmp_path / "repolace-task-fresh"
    other = tmp_path / "unrelated-old"
    for d in (old, fresh, other):
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "f").write_text("x")
    long_ago = time.time() - 10_000
    for d in (old, other):
        os.utime(d, (long_ago, long_ago))

    removed = remove_stale_workspaces(tmp_path, 3_600)

    assert removed == [old]
    assert not old.exists() and fresh.exists() and other.exists()


def test_a_missing_parent_is_not_an_error(tmp_path):
    assert remove_stale_workspaces(tmp_path / "nope", 1) == []
