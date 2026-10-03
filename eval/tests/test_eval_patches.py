"""`harness.patches`: which files a diff touches and which OLD lines it changes.

Most diffs here are produced by real `git diff`, so quoting, rename detection and
the `\\ No newline` marker are git's own output rather than my memory of it. The
hand-written ones are the shapes git will not produce on demand.
"""

from pathlib import Path

import pytest

from eval_support import commit_files, diff_between, make_repo
from harness.patches import FileChange, PatchError, files_in_patch, old_side_hunks

LINES = [f"l{i}" for i in range(1, 31)]


def text(lines: list[str], *, newline_at_end: bool = True) -> str:
    return "\n".join(lines) + ("\n" if newline_at_end else "")


def diff_of(tmp_path: Path, before: dict, after: dict, *, renames: bool = False) -> str:
    repo = tmp_path / "repo"
    base = make_repo(repo, before)
    return diff_between(repo, base, commit_files(repo, after, "change"), renames=renames)


class TestStatuses:
    def test_added_modified_and_deleted(self, tmp_path):
        diff = diff_of(
            tmp_path,
            {"keep.py": text(LINES), "gone.py": "x = 1\n", "same.py": "y = 1\n"},
            {"keep.py": text([*LINES[:-1], "changed"]), "gone.py": None, "fresh.py": "z = 1\n"},
        )
        assert files_in_patch(diff) == [
            FileChange("fresh.py", "added"),
            FileChange("gone.py", "deleted"),
            FileChange("keep.py", "modified"),
        ]

    def test_rename_carries_the_old_path(self, tmp_path):
        diff = diff_of(
            tmp_path,
            {"old_name.py": text(LINES)},
            {"old_name.py": None, "new_name.py": text(LINES)},
            renames=True,
        )
        assert files_in_patch(diff) == [FileChange("new_name.py", "renamed", old_path="old_name.py")]

    def test_rename_with_an_edit_keeps_both_the_status_and_the_hunks(self, tmp_path):
        diff = diff_of(
            tmp_path,
            {"old_name.py": text(LINES)},
            {"old_name.py": None, "new_name.py": text([*LINES[:9], "edited", *LINES[10:]])},
            renames=True,
        )
        assert files_in_patch(diff) == [FileChange("new_name.py", "renamed", old_path="old_name.py")]
        # Keyed by the path the lines have at the base commit.
        assert old_side_hunks(diff) == {"old_name.py": [(10, 10)]}

    def test_a_binary_file_is_binary_whatever_was_done_to_it(self, tmp_path):
        diff = diff_of(
            tmp_path,
            {"logo.png": b"\x89PNG\x00old", "text.py": "a\n"},
            {"logo.png": b"\x89PNG\x00new", "extra.bin": b"\x00\x01", "text.py": "b\n"},
        )
        by_path = {c.path: c.status for c in files_in_patch(diff)}
        assert by_path == {"extra.bin": "binary", "logo.png": "binary", "text.py": "modified"}

    def test_a_mode_change_alone_is_a_modification_with_no_old_side_lines(self):
        diff = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
        assert files_in_patch(diff) == [FileChange("run.sh", "modified")]
        assert old_side_hunks(diff) == {}

    def test_a_copy_is_an_addition_that_remembers_its_source(self):
        diff = (
            "diff --git a/src.py b/dst.py\nsimilarity index 100%\ncopy from src.py\ncopy to dst.py\n"
        )
        assert files_in_patch(diff) == [FileChange("dst.py", "added", old_path="src.py")]

    def test_an_empty_new_file_has_no_hunks_but_is_still_added(self):
        diff = "diff --git a/pkg/__init__.py b/pkg/__init__.py\nnew file mode 100644\nindex 0000000..e69de29\n"
        assert files_in_patch(diff) == [FileChange("pkg/__init__.py", "added")]

    def test_an_empty_diff_touches_nothing(self):
        assert files_in_patch("") == []
        assert files_in_patch("\n") == []
        assert old_side_hunks("") == {}

    def test_a_mail_preamble_before_the_first_block_is_ignored(self):
        diff = (
            "From abc Mon Sep 17 00:00:00 2001\nSubject: [PATCH] x\n\n---\n"
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        )
        assert files_in_patch(diff) == [FileChange("a.py", "modified")]


class TestOldSideRanges:
    def test_changed_old_lines_not_the_context_around_them(self, tmp_path):
        after = list(LINES)
        after[4] = "X5"  # replace l5
        after[27] = "X28"  # replace l28
        after.insert(20, "new")  # insert after l20
        for gone in ("l13", "l12"):  # delete l12 and l13
            after.remove(gone)
        diff = diff_of(tmp_path, {"m.py": text(LINES)}, {"m.py": text(after)})
        assert old_side_hunks(diff) == {"m.py": [(5, 5), (12, 13), (20, 20), (28, 28)]}

    def test_a_pure_insertion_is_anchored_to_the_old_line_it_follows(self, tmp_path):
        after = [*LINES[:7], "inserted", *LINES[7:]]  # between l7 and l8
        diff = diff_of(tmp_path, {"m.py": text(LINES)}, {"m.py": text(after)})
        assert old_side_hunks(diff) == {"m.py": [(7, 7)]}

    def test_an_insertion_at_the_top_is_anchored_to_line_one(self, tmp_path):
        diff = diff_of(tmp_path, {"m.py": text(LINES)}, {"m.py": text(["top", *LINES])})
        assert old_side_hunks(diff) == {"m.py": [(1, 1)]}

    def test_an_insertion_at_the_end_follows_the_last_line(self, tmp_path):
        diff = diff_of(tmp_path, {"m.py": text(LINES)}, {"m.py": text([*LINES, "tail"])})
        assert old_side_hunks(diff) == {"m.py": [(30, 30)]}

    def test_a_deleted_file_is_removed_in_full(self, tmp_path):
        diff = diff_of(tmp_path, {"m.py": text(LINES[:6])}, {"m.py": None})
        assert old_side_hunks(diff) == {"m.py": [(1, 6)]}

    def test_a_new_file_has_no_old_side(self, tmp_path):
        diff = diff_of(tmp_path, {"a.py": "x\n"}, {"b.py": text(LINES)})
        assert old_side_hunks(diff) == {}

    def test_two_files_each_keep_their_own_ranges(self, tmp_path):
        before = {"a.py": text(LINES), "b.py": text(LINES)}
        diff = diff_of(
            tmp_path, before,
            {"a.py": text(["A", *LINES[1:]]), "b.py": text([*LINES[:29], "B"])},
        )
        assert old_side_hunks(diff) == {"a.py": [(1, 1)], "b.py": [(30, 30)]}

    def test_the_no_newline_marker_is_not_a_line_on_either_side(self, tmp_path):
        before = {"m.py": text(LINES, newline_at_end=False)}
        after = {"m.py": text([*LINES[:-1], "tail"], newline_at_end=False)}
        diff = diff_of(tmp_path, before, after)
        assert "\\ No newline at end of file" in diff
        assert old_side_hunks(diff) == {"m.py": [(30, 30)]}

    def test_a_hunk_header_without_counts_means_one_line(self):
        diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -3 +3 @@\n-old\n+new\n"
        assert old_side_hunks(diff) == {"a.py": [(3, 3)]}

    def test_a_context_line_with_its_space_stripped_still_counts(self):
        diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n a\n\n-c\n+C\n"
        assert old_side_hunks(diff) == {"a.py": [(3, 3)]}


class TestContentIsNeverHeader:
    def test_removed_and_added_lines_that_look_like_file_headers(self, tmp_path):
        # A removed line `-- a/evil.py` is written `--- a/evil.py`; an added line
        # `++ b/evil.py` is written `+++ b/evil.py`.
        before = {"notes.py": "keep\n-- a/evil.py\nkeep2\n", "other.py": "o\n"}
        after = {"notes.py": "keep\n++ b/evil.py\nkeep2\n", "other.py": "o2\n"}
        diff = diff_of(tmp_path, before, after)
        assert "--- a/evil.py" in diff and "+++ b/evil.py" in diff
        assert [c.path for c in files_in_patch(diff)] == ["notes.py", "other.py"]
        assert old_side_hunks(diff) == {"notes.py": [(2, 2)], "other.py": [(1, 1)]}

    def test_a_form_feed_in_the_source_does_not_shift_the_counts(self, tmp_path):
        # str.splitlines() would split on \x0c, \x85 and U+2028 and miscount the hunk.
        before = {"m.py": "a\n\x0c\nb\x85c\nd e\nf\n"}
        after = {"m.py": "a\n\x0c\nb\x85c\nd e\nF\n"}
        diff = diff_of(tmp_path, before, after)
        assert old_side_hunks(diff) == {"m.py": [(5, 5)]}

    def test_carriage_returns_in_content_are_part_of_the_line(self, tmp_path):
        before = {"crlf.py": "a\r\nb\r\nc\r\n"}
        after = {"crlf.py": "a\r\nB\r\nc\r\n"}
        diff = diff_of(tmp_path, before, after)
        assert old_side_hunks(diff) == {"crlf.py": [(2, 2)]}


class TestQuotedPaths:
    @pytest.mark.parametrize(
        "name",
        ["has space.py", "café.py", 'quo"te.py', "tab\there.py", "dir with space/in dir.py"],
    )
    def test_git_quoting_round_trips(self, tmp_path, name):
        diff = diff_of(tmp_path, {name: "a\n"}, {name: "b\n"})
        assert files_in_patch(diff) == [FileChange(name, "modified")]
        assert old_side_hunks(diff) == {name: [(1, 1)]}

    def test_a_quoted_new_file_and_a_quoted_deleted_file(self, tmp_path):
        diff = diff_of(tmp_path, {"gone café.py": "a\n"}, {"gone café.py": None, "new café.py": "b\n"})
        assert files_in_patch(diff) == [
            FileChange("gone café.py", "deleted"),
            FileChange("new café.py", "added"),
        ]

    def test_a_mode_only_block_with_a_space_in_the_name_uses_the_header_line(self):
        diff = "diff --git a/my dir/run it.sh b/my dir/run it.sh\nold mode 100644\nnew mode 100755\n"
        assert files_in_patch(diff) == [FileChange("my dir/run it.sh", "modified")]

    def test_a_quoted_binary_file_is_named_from_the_header(self, tmp_path):
        diff = diff_of(tmp_path, {"imáge.png": b"\x00a"}, {"imáge.png": b"\x00b"})
        assert files_in_patch(diff) == [FileChange("imáge.png", "binary")]


class TestRefusals:
    def test_text_that_is_not_a_diff(self):
        with pytest.raises(PatchError, match="no 'diff --git' header"):
            files_in_patch("this is a sentence, not a patch\n")

    def test_a_truncated_hunk(self):
        diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,4 +1,4 @@\n a\n-b\n+B\n"
        with pytest.raises(PatchError, match="truncated"):
            old_side_hunks(diff)

    def test_a_hunk_with_more_removed_lines_than_its_header_says(self):
        diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n-b\n+c\n"
        with pytest.raises(PatchError, match="more '-' lines"):
            files_in_patch(diff)

    def test_a_line_that_belongs_to_no_hunk_shape(self):
        diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n a\n?what\n"
        with pytest.raises(PatchError, match="unexpected line"):
            files_in_patch(diff)

    def test_a_path_without_the_a_b_prefixes_is_refused_not_guessed(self):
        diff = "diff --git a.py b.py\n--- a.py\n+++ b.py\n@@ -1 +1 @@\n-x\n+y\n"
        with pytest.raises(PatchError):
            files_in_patch(diff)

    def test_a_bad_escape_in_a_quoted_path(self):
        diff = 'diff --git "a/x\\q.py" "b/x\\q.py"\nnew file mode 100644\n'
        with pytest.raises(PatchError, match="unknown escape"):
            files_in_patch(diff)

    def test_an_octal_escape_that_is_not_utf8(self):
        diff = 'diff --git "a/x\\377.py" "b/x\\377.py"\nnew file mode 100644\n'
        with pytest.raises(PatchError, match="not valid UTF-8"):
            files_in_patch(diff)

    def test_a_rename_header_with_only_one_side(self):
        diff = "diff --git a/a.py b/b.py\nrename from a.py\n"
        with pytest.raises(PatchError, match="unpaired"):
            files_in_patch(diff)

    def test_two_different_paths_without_a_rename_header(self):
        diff = "diff --git a/a.py b/b.py\n--- a/a.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
        with pytest.raises(PatchError, match="without a rename"):
            files_in_patch(diff)
