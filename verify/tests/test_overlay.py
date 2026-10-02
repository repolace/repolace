"""`apply_overlay`: hidden-test files onto an export, and every way it must refuse.

The keys come from instance data, but they become paths on the host filesystem,
so each refusal below is a way for that data -- or a bug in whatever prepared it --
to write somewhere it must not. Every refusal also asserts that **nothing was
written**: an overlay that fails half way would leave the export with some of the
hidden tests and not others, and the run would score against a test set nobody
chose.
"""

import os
import stat
from pathlib import Path

import pytest

from verify.overlay import OverlayError, apply_overlay

DIR_MODE = 0o777


def tree(root: Path) -> dict[str, bytes]:
    """Every regular file under `root`, by relative path -- what a refusal must leave unchanged."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


@pytest.fixture
def export(tmp_path):
    root = tmp_path / "export-0"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_bytes(b"print('app')\n")
    return root


class TestWriting:
    def test_the_bytes_round_trip_exactly(self, export):
        """Not text: CRLF, a missing final newline and non-UTF-8 bytes must all survive."""
        payload = b"\xff\xfe\x00line one\r\nline two"

        apply_overlay(export, {"tests/test_hidden.py": payload}, dir_mode=DIR_MODE)

        assert (export / "tests" / "test_hidden.py").read_bytes() == payload

    def test_empty_content_is_a_file_not_a_skip(self, export):
        apply_overlay(export, {"tests/__init__.py": b""}, dir_mode=DIR_MODE)

        assert (export / "tests" / "__init__.py").read_bytes() == b""

    def test_several_files_and_nested_directories(self, export):
        files = {"tests/a/b/test_deep.py": b"1", "tests/conftest.py": b"2", "tests/data/x.json": b"{}"}

        apply_overlay(export, files, dir_mode=DIR_MODE)

        assert tree(export) == {"src/app.py": b"print('app')\n", **files}

    def test_an_existing_file_is_replaced_in_full(self, export):
        """Never appended to or patched: the instance stores complete file bytes."""
        (export / "tests").mkdir()
        (export / "tests" / "test_x.py").write_bytes(b"old content that is much longer than the new")

        apply_overlay(export, {"tests/test_x.py": b"new"}, dir_mode=DIR_MODE)

        assert (export / "tests" / "test_x.py").read_bytes() == b"new"

    def test_it_touches_only_the_paths_it_was_given(self, export):
        apply_overlay(export, {"tests/test_x.py": b"x"}, dir_mode=DIR_MODE)

        assert (export / "src" / "app.py").read_bytes() == b"print('app')\n"

    def test_no_files_is_a_no_op_even_without_a_directory(self, tmp_path):
        apply_overlay(tmp_path / "does-not-exist", {}, dir_mode=DIR_MODE)

    def test_a_missing_target_directory_is_refused_when_there_is_something_to_write(self, tmp_path):
        with pytest.raises(OverlayError, match="does not exist"):
            apply_overlay(tmp_path / "gone", {"t.py": b"x"}, dir_mode=DIR_MODE)


class TestModes:
    def test_files_are_0644(self, export):
        apply_overlay(export, {"tests/test_x.py": b"x"}, dir_mode=DIR_MODE)

        assert stat.S_IMODE((export / "tests" / "test_x.py").stat().st_mode) == 0o644

    def test_a_replaced_executable_is_0644_too(self, export):
        """The export gives a tracked script 0755; the overlay replaces the file
        and its mode with the documented one."""
        target = export / "run.sh"
        target.write_bytes(b"#!/bin/sh\n")
        target.chmod(0o755)

        apply_overlay(export, {"run.sh": b"#!/bin/sh\necho hi\n"}, dir_mode=DIR_MODE)

        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    def test_a_restrictive_umask_does_not_make_the_file_unreadable(self, export):
        """The sandbox uid is not the owner, so 0600 here is an unscoreable run."""
        previous = os.umask(0o077)
        try:
            apply_overlay(export, {"tests/test_x.py": b"x"}, dir_mode=DIR_MODE)
        finally:
            os.umask(previous)

        assert stat.S_IMODE((export / "tests" / "test_x.py").stat().st_mode) == 0o644

    def test_created_directories_get_the_requested_mode_all_the_way_down(self, export):
        """`mkdir`'s mode argument is masked by the umask. Under 022 a plain
        `mkdir(mode=0o777)` yields 0o755, and the sandbox uid then cannot write a
        `__pycache__` beside its own tests."""
        previous = os.umask(0o022)
        try:
            apply_overlay(export, {"tests/a/b/test_deep.py": b"x"}, dir_mode=DIR_MODE)
        finally:
            os.umask(previous)

        for directory in ("tests", "tests/a", "tests/a/b"):
            assert stat.S_IMODE((export / directory).stat().st_mode) == DIR_MODE, directory

    def test_a_different_dir_mode_is_honoured(self, export):
        apply_overlay(export, {"tests/test_x.py": b"x"}, dir_mode=0o750)

        assert stat.S_IMODE((export / "tests").stat().st_mode) == 0o750

    def test_a_directory_that_already_exists_is_left_as_it_is(self, export):
        (export / "tests").mkdir()
        (export / "tests").chmod(0o700)

        apply_overlay(export, {"tests/test_x.py": b"x"}, dir_mode=DIR_MODE)

        assert stat.S_IMODE((export / "tests").stat().st_mode) == 0o700


class TestRefusals:
    @pytest.mark.parametrize(
        ("key", "reason"),
        [
            ("/etc/cron.d/x", "absolute"),
            ("../escape.py", "'..'"),
            ("tests/../../escape.py", "'..'"),
            ("tests/..", "'..'"),
            (".git/hooks/post-commit", ".git"),
            ("tests/.git/config", ".git"),
            (".git", ".git"),
            (".GIT/hooks/x", ".git"),
            ("sub/.Git/x", ".git"),
            ("tests\\test_x.py", "backslash"),
            ("tests\\..\\..\\x", "backslash"),
            ("tests/te\x00st.py", "NUL"),
            ("", "non-empty"),
            ("./tests/test_x.py", "canonical"),
            ("tests//test_x.py", "canonical"),
            ("tests/", "canonical"),
            (".", "does not name a file"),
        ],
    )
    def test_a_hostile_or_malformed_path_is_refused_and_named(self, export, key, reason):
        before = tree(export)

        with pytest.raises(OverlayError, match=reason) as excinfo:
            apply_overlay(export, {key: b"x"}, dir_mode=DIR_MODE)

        assert excinfo.value.path == key
        assert repr(key) in str(excinfo.value)
        assert tree(export) == before

    def test_a_symlinked_parent_that_points_outside_is_refused(self, export, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (export / "tests").symlink_to(outside)

        with pytest.raises(OverlayError, match="tests"):
            apply_overlay(export, {"tests/test_x.py": b"x"}, dir_mode=DIR_MODE)

        assert list(outside.iterdir()) == []

    def test_a_symlinked_parent_that_points_back_inside_is_refused_too(self, export):
        """`resolve_within` would allow this -- the target is inside the tree. But
        following it is still letting the export choose where a write goes."""
        (export / "tests").symlink_to(export / "src")

        with pytest.raises(OverlayError, match="symlink"):
            apply_overlay(export, {"tests/test_x.py": b"x"}, dir_mode=DIR_MODE)

        assert not (export / "src" / "test_x.py").exists()

    def test_a_symlink_at_the_target_is_refused_not_written_through(self, export, tmp_path):
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"precious")
        (export / "tests").mkdir()
        (export / "tests" / "test_x.py").symlink_to(victim)

        with pytest.raises(OverlayError, match="symlink"):
            apply_overlay(export, {"tests/test_x.py": b"overwritten"}, dir_mode=DIR_MODE)

        assert victim.read_bytes() == b"precious"

    def test_a_dangling_symlinked_parent_is_refused(self, export, tmp_path):
        (export / "tests").symlink_to(tmp_path / "nowhere")

        with pytest.raises(OverlayError, match="symlink"):
            apply_overlay(export, {"tests/test_x.py": b"x"}, dir_mode=DIR_MODE)

        assert not (tmp_path / "nowhere").exists()

    def test_a_parent_that_is_a_regular_file_is_refused(self, export):
        with pytest.raises(OverlayError, match="not a directory"):
            apply_overlay(export, {"src/app.py/test_x.py": b"x"}, dir_mode=DIR_MODE)

    def test_a_path_that_is_an_existing_directory_is_refused(self, export):
        with pytest.raises(OverlayError, match="existing directory"):
            apply_overlay(export, {"src": b"x"}, dir_mode=DIR_MODE)

    def test_a_file_and_one_beneath_it_cannot_both_be_written(self, export):
        before = tree(export)

        with pytest.raises(OverlayError, match="also an overlay file"):
            apply_overlay(export, {"tests": b"x", "tests/test_x.py": b"y"}, dir_mode=DIR_MODE)

        assert tree(export) == before

    def test_content_must_be_bytes(self, export):
        """A str would be encoded by whatever default is in force, and the file
        on disk would no longer be what the instance recorded."""
        with pytest.raises(OverlayError, match="bytes"):
            apply_overlay(export, {"tests/test_x.py": "text"}, dir_mode=DIR_MODE)  # type: ignore[dict-item]

    def test_a_non_string_key_is_refused(self, export):
        with pytest.raises(OverlayError):
            apply_overlay(export, {b"tests/test_x.py": b"x"}, dir_mode=DIR_MODE)  # type: ignore[dict-item]


class TestAllOrNothing:
    def test_one_bad_entry_means_no_entry_is_written(self, export):
        """Valid entries come first in dict order, so a validate-as-you-go
        implementation would have written them before reaching the bad one."""
        before = tree(export)
        files = {"tests/test_good.py": b"1", "tests/data/x.json": b"2", "../escape.py": b"3"}

        with pytest.raises(OverlayError):
            apply_overlay(export, files, dir_mode=DIR_MODE)

        assert tree(export) == before
        assert not (export / "tests").exists()

    def test_the_failure_names_the_bad_entry_not_a_good_one(self, export):
        files = {"tests/test_good.py": b"1", ".git/config": b"2"}

        with pytest.raises(OverlayError) as excinfo:
            apply_overlay(export, files, dir_mode=DIR_MODE)

        assert excinfo.value.path == ".git/config"

    def test_an_overlay_error_is_a_value_error(self):
        """So a caller holding instance data can treat it as bad input."""
        assert issubclass(OverlayError, ValueError)
