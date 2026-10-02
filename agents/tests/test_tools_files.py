"""`read_file`, `list_dir`, `edit_file`, `create_file`: what each does, and that each refusal changes nothing.

Run through `ToolBox.dispatch` on a real checkout, so the schema bounds, the path
guard and the tool all act together, as they do in a run.
"""

import os
import stat

import pytest

from repolace_agents.tools import ToolLimits

from tools_support import git, make_checkout, make_harness, snapshot

pytestmark = pytest.mark.anyio


@pytest.fixture
def h(tmp_path):
    return make_harness(tmp_path)


def lines_of(count: int) -> str:
    return "".join(f"line {number}\n" for number in range(1, count + 1))


class TestReadFile:
    async def test_it_shows_numbered_lines_under_the_canonical_path(self, h):
        out = await h.call("read_file", path="./src/pkg/core.py")

        assert not out.is_error
        assert out.content.splitlines()[0] == "src/pkg/core.py (lines 1-6 of 6)"
        assert "     1\tdef add(a, b):" in out.content
        assert "     6\t    return a - b" in out.content

    async def test_a_range_shows_only_those_lines(self, h):
        (h.checkout / "src/pkg/util.py").write_text(lines_of(10))

        out = await h.call("read_file", path="src/pkg/util.py", start_line=3, end_line=5)

        assert "lines 3-5 of 10" in out.content
        assert "line 3" in out.content and "line 5" in out.content
        assert "line 2" not in out.content and "line 6" not in out.content
        assert "more line" not in out.content

    async def test_it_caps_the_lines_per_call_and_says_where_to_continue(self, tmp_path):
        h = make_harness(tmp_path, limits=ToolLimits(max_read_lines=10, max_output_chars=8000))
        (h.checkout / "big.txt").write_text(lines_of(25))

        out = await h.call("read_file", path="big.txt")

        assert "lines 1-10 of 25" in out.content
        assert "line 11" not in out.content
        assert "15 more line(s)" in out.content and "start_line=11" in out.content

    async def test_an_end_line_past_the_cap_is_clipped_to_it(self, tmp_path):
        h = make_harness(tmp_path, limits=ToolLimits(max_read_lines=10))
        (h.checkout / "big.txt").write_text(lines_of(25))

        out = await h.call("read_file", path="big.txt", start_line=5, end_line=25)

        assert "lines 5-14 of 25" in out.content and "start_line=15" in out.content

    async def test_it_shows_the_file_as_it_is_now_not_as_committed(self, h):
        await h.call("edit_file", path="src/pkg/util.py", old_string="VALUE = 1", new_string="VALUE = 2")

        out = await h.call("read_file", path="src/pkg/util.py")

        assert "VALUE = 2" in out.content

    async def test_a_form_feed_does_not_split_a_line(self, h):
        (h.checkout / "src/pkg/util.py").write_text("a\x0cb\nc\n")

        out = await h.call("read_file", path="src/pkg/util.py")

        assert "of 2)" in out.content

    async def test_a_file_without_a_trailing_newline_counts_its_last_line(self, h):
        (h.checkout / "src/pkg/util.py").write_text("one\ntwo")

        assert "of 2)" in (await h.call("read_file", path="src/pkg/util.py")).content

    async def test_an_empty_file_says_so(self, h):
        out = await h.call("read_file", path="src/pkg/__init__.py")

        assert not out.is_error and "is empty" in out.content

    async def test_bytes_that_are_not_utf8_are_shown_not_fatal(self, h):
        (h.checkout / "src/pkg/util.py").write_bytes(b"caf\xe9 = 1\n")

        out = await h.call("read_file", path="src/pkg/util.py")

        assert not out.is_error and "caf" in out.content

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"path": "src/pkg/core.py", "start_line": 99}, "only 6 line"),
            ({"path": "src/pkg/core.py", "start_line": 4, "end_line": 2}, "before start_line"),
            ({"path": "src/nope.py"}, "does not exist"),
            ({"path": "src/pkg"}, "is a directory"),
            ({"path": "src/pkg/core.py/x"}, "does not exist"),
        ],
    )
    async def test_it_refuses_what_it_cannot_show(self, h, kwargs, message):
        out = await h.call("read_file", **kwargs)

        assert out.is_error and message in out.content

    async def test_it_refuses_a_binary_file(self, h):
        (h.checkout / "blob.bin").write_bytes(b"\x89PNG\r\n\x00\x00data")

        out = await h.call("read_file", path="blob.bin")

        assert out.is_error and "binary" in out.content

    async def test_it_refuses_a_file_over_the_size_limit(self, tmp_path):
        h = make_harness(tmp_path, limits=ToolLimits(max_file_bytes=100))
        (h.checkout / "huge.txt").write_text("x" * 101)

        out = await h.call("read_file", path="huge.txt")

        assert out.is_error and "100-byte limit" in out.content

    @pytest.mark.parametrize("path", [".git/config", ".gitattributes", ".github/workflows/ci.yml", "../outside.txt", "/etc/passwd"])
    async def test_it_refuses_git_internals_and_escapes(self, h, path):
        assert (await h.call("read_file", path=path)).is_error

    async def test_it_does_not_follow_a_symlink_to_a_host_file(self, h, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("TOKEN=hunter2\n")
        os.symlink(secret, h.checkout / "settings.py")

        out = await h.call("read_file", path="settings.py")

        assert out.is_error and "hunter2" not in out.content


class TestListDir:
    async def test_it_lists_the_root_marking_directories(self, h):
        out = await h.call("list_dir")

        entries = out.content.splitlines()[1:]
        assert entries == sorted(entries) == ["README.md", "conftest.py", "pyproject.toml", "src/", "tests/"]

    async def test_it_hides_dot_git_and_the_rest_of_the_family(self, h):
        (h.checkout / ".github").mkdir()

        entries = (await h.call("list_dir")).content.splitlines()

        assert not any(entry.startswith(".git") for entry in entries)

    async def test_depth_two_and_three_include_subdirectories(self, h):
        two = (await h.call("list_dir", path="src", depth=2)).content
        three = (await h.call("list_dir", depth=3)).content

        assert two.splitlines() == ["src/", "pkg/", "pkg/__init__.py", "pkg/core.py", "pkg/util.py"]
        assert "src/pkg/core.py" in three or "pkg/core.py" in three.splitlines()

    async def test_depth_one_does_not_descend(self, h):
        assert "core.py" not in (await h.call("list_dir", path="src")).content

    async def test_depth_beyond_three_is_refused_by_the_schema(self, h):
        out = await h.call("list_dir", depth=4)

        assert out.is_error and "at most 3" in out.content

    async def test_it_caps_the_entries(self, h):
        for number in range(400):
            (h.checkout / f"f{number:03}.txt").write_text("x")

        out = await h.call("list_dir")

        lines = out.content.splitlines()
        assert len(lines[1:-1]) == 300 and "more than 300 entries" in lines[-1]

    async def test_it_shows_a_symlink_without_following_it(self, h, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x")
        os.symlink(outside, h.checkout / "link")

        out = await h.call("list_dir", depth=3)

        assert "link@" in out.content and "secret.txt" not in out.content
        assert (await h.call("list_dir", path="link")).is_error

    @pytest.mark.parametrize(("path", "message"), [("src/pkg/core.py", "not a directory"), ("nope", "does not exist")])
    async def test_it_refuses_what_is_not_a_directory(self, h, path, message):
        out = await h.call("list_dir", path=path)

        assert out.is_error and message in out.content

    @pytest.mark.parametrize("path", [".git", ".git/hooks", "../", "/"])
    async def test_it_refuses_git_internals_and_escapes(self, h, path):
        assert (await h.call("list_dir", path=path)).is_error

    async def test_a_file_name_that_is_not_utf8_is_shown_escaped_not_raw(self, h):
        # Python decodes such a name with surrogate escapes; echoed raw it would fail to
        # encode in the next provider request.
        (h.checkout / os.fsdecode(b"caf\xe9.py")).write_text("x = 1\n")

        out = await h.call("list_dir")

        out.content.encode("utf-8")
        assert "caf\\udce9.py" in out.content

    async def test_an_empty_directory_says_so(self, h):
        (h.checkout / "empty").mkdir()

        out = await h.call("list_dir", path="empty")

        assert not out.is_error and "is empty" in out.content


class TestEditFile:
    async def test_a_unique_match_is_replaced_on_disk(self, h):
        out = await h.call("edit_file", path="src/pkg/core.py", old_string="return a + b", new_string="return b + a")

        assert not out.is_error
        assert (h.checkout / "src/pkg/core.py").read_text() == "def add(a, b):\n    return b + a\n\n\ndef sub(a, b):\n    return a - b\n"

    async def test_the_result_shows_the_edit_with_three_lines_of_context(self, h):
        (h.checkout / "src/pkg/util.py").write_text(lines_of(20))

        out = await h.call("edit_file", path="src/pkg/util.py", old_string="line 10\n", new_string="TEN\nTEN2\n")

        body = out.content.splitlines()
        assert body[0] == "edited src/pkg/util.py"
        assert [line.split("\t")[1] for line in body[1:]] == [
            "line 7", "line 8", "line 9", "TEN", "TEN2", "line 11", "line 12", "line 13",
        ]
        assert body[1].startswith("     7\t")

    async def test_zero_matches_is_an_error_and_changes_nothing(self, h):
        before = snapshot(h.checkout)

        out = await h.call("edit_file", path="src/pkg/core.py", old_string="return a * b", new_string="x")

        assert out.is_error and "not found" in out.content and "read_file" in out.content
        assert snapshot(h.checkout) == before

    async def test_several_matches_is_an_error_naming_the_count(self, h):
        before = snapshot(h.checkout)

        out = await h.call("edit_file", path="src/pkg/core.py", old_string="return", new_string="yield")

        assert out.is_error and "2 times" in out.content and "replace_all" in out.content
        assert snapshot(h.checkout) == before

    async def test_replace_all_changes_every_occurrence(self, h):
        out = await h.call("edit_file", path="src/pkg/core.py", old_string="return", new_string="yield", replace_all=True)

        assert not out.is_error and "replaced 2 occurrences" in out.content
        assert (h.checkout / "src/pkg/core.py").read_text().count("yield") == 2

    async def test_identical_old_and_new_is_an_error(self, h):
        out = await h.call("edit_file", path="src/pkg/core.py", old_string="return a + b", new_string="return a + b")

        assert out.is_error and "identical" in out.content

    async def test_it_keeps_the_file_mode_and_other_line_endings(self, h):
        path = h.checkout / "src/pkg/util.py"
        path.write_bytes(b"A = 1\r\nB = 2\r\n")
        path.chmod(0o755)

        await h.call("edit_file", path="src/pkg/util.py", old_string="A = 1", new_string="A = 9")

        assert path.read_bytes() == b"A = 9\r\nB = 2\r\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o755

    async def test_a_missing_file_is_an_error_and_is_not_created(self, h):
        out = await h.call("edit_file", path="src/pkg/nope.py", old_string="a", new_string="b")

        assert out.is_error and "does not exist" in out.content
        assert not (h.checkout / "src/pkg/nope.py").exists()

    async def test_a_binary_file_is_refused(self, h):
        (h.checkout / "src/blob.py").write_bytes(b"a\x00b")

        out = await h.call("edit_file", path="src/blob.py", old_string="a", new_string="c")

        assert out.is_error and "binary" in out.content
        assert (h.checkout / "src/blob.py").read_bytes() == b"a\x00b"

    async def test_a_file_that_is_not_utf8_is_refused_rather_than_corrupted(self, h):
        (h.checkout / "src/latin.py").write_bytes(b"caf\xe9 = 1\n")

        out = await h.call("edit_file", path="src/latin.py", old_string="= 1", new_string="= 2")

        assert out.is_error and "UTF-8" in out.content
        assert (h.checkout / "src/latin.py").read_bytes() == b"caf\xe9 = 1\n"

    async def test_a_nul_in_the_replacement_is_refused(self, h):
        out = await h.call("edit_file", path="src/pkg/util.py", old_string="1", new_string="1\x00")

        assert out.is_error and "NUL" in out.content
        assert (h.checkout / "src/pkg/util.py").read_text() == "VALUE = 1\n"

    async def test_a_replace_all_that_would_blow_up_the_file_is_refused_before_it_is_built(self, tmp_path):
        h = make_harness(tmp_path, limits=ToolLimits(max_file_bytes=1000, max_edit_chars=20_000))
        (h.checkout / "src/a.py").write_text("a" * 500)

        out = await h.call("edit_file", path="src/a.py", old_string="a", new_string="b" * 100, replace_all=True)

        assert out.is_error and "1000-byte limit" in out.content
        assert (h.checkout / "src/a.py").read_text() == "a" * 500

    async def test_oversized_strings_are_refused_by_the_schema(self, h):
        out = await h.call("edit_file", path="src/pkg/util.py", old_string="x" * 20_001, new_string="y")

        assert out.is_error and "at most 20000" in out.content

    @pytest.mark.parametrize(
        "path",
        ["tests/test_core.py", "conftest.py", "pyproject.toml", "./tests/test_core.py"],
    )
    async def test_protected_files_are_refused_and_unchanged(self, h, path):
        before = snapshot(h.checkout)

        out = await h.call("edit_file", path=path, old_string="a", new_string="b")

        assert out.is_error and "read-only" in out.content
        assert snapshot(h.checkout) == before

    async def test_the_guard_is_the_one_the_pipeline_injected(self, tmp_path):
        h = make_harness(tmp_path, is_protected=lambda rel: rel == "src/pkg/util.py")

        assert (await h.call("edit_file", path="src/pkg/util.py", old_string="1", new_string="2")).is_error
        assert not (await h.call("edit_file", path="src/pkg/core.py", old_string="a + b", new_string="b + a")).is_error

    @pytest.mark.parametrize("path", [".git/config", ".git/hooks/post-commit", ".gitattributes", ".gitignore", ".github/workflows/ci.yml"])
    async def test_dot_git_paths_are_refused_and_unchanged(self, h, path):
        before = snapshot(h.checkout)

        out = await h.call("edit_file", path=path, old_string="a", new_string="b")

        assert out.is_error
        assert snapshot(h.checkout) == before

    async def test_it_will_not_write_through_a_symlink(self, h, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("TOKEN=hunter2\n")
        os.symlink(secret, h.checkout / "src/linked.py")

        out = await h.call("edit_file", path="src/linked.py", old_string="hunter2", new_string="pwned")

        assert out.is_error
        assert secret.read_text() == "TOKEN=hunter2\n"


class TestCreateFile:
    async def test_it_creates_the_file_and_its_parents(self, h):
        out = await h.call("create_file", path="src/pkg/new/deep/mod.py", content="X = 1\n")

        assert not out.is_error and out.content.startswith("created src/pkg/new/deep/mod.py (1 lines)")
        assert (h.checkout / "src/pkg/new/deep/mod.py").read_text() == "X = 1\n"

    async def test_an_existing_file_is_refused_and_unchanged(self, h):
        before = snapshot(h.checkout)

        out = await h.call("create_file", path="src/pkg/util.py", content="OVERWRITTEN\n")

        assert out.is_error and "already exists" in out.content and "edit_file" in out.content
        assert snapshot(h.checkout) == before

    async def test_an_empty_file_may_be_created(self, h):
        assert not (await h.call("create_file", path="src/pkg/empty.py", content="")).is_error
        assert (h.checkout / "src/pkg/empty.py").read_bytes() == b""

    async def test_the_new_file_is_an_ordinary_non_executable_file(self, h):
        await h.call("create_file", path="src/pkg/new.py", content="X = 1\n")

        assert stat.S_IMODE((h.checkout / "src/pkg/new.py").stat().st_mode) & 0o111 == 0

    @pytest.mark.parametrize("path", ["debug.log", "build/out.py", "src/pkg/trace.log"])
    async def test_a_git_ignored_path_is_created_with_a_warning(self, h, path):
        out = await h.call("create_file", path=path, content="x\n")

        assert not out.is_error and "WARNING" in out.content and "never be committed" in out.content
        assert (h.checkout / path).exists()

    @pytest.mark.parametrize("name", [":(exclude)x.py", "-flag.py", "[a]*.py", "--help"])
    async def test_a_name_shaped_like_pathspec_magic_or_an_option_is_just_a_name(self, h, name):
        # Reaches `git check-ignore` as an operand after `--`, behind a `./`, never as magic or a flag.
        out = await h.call("create_file", path=name, content="x = 1\n")

        assert not out.is_error, out.content
        assert (h.checkout / name).read_text() == "x = 1\n"
        assert "WARNING" not in out.content

    async def test_a_path_that_is_not_ignored_has_no_warning(self, h):
        out = await h.call("create_file", path="src/pkg/ok.py", content="x\n")

        assert "WARNING" not in out.content

    async def test_the_size_cap_is_enforced_and_nothing_is_created(self, h):
        before = snapshot(h.checkout)

        out = await h.call("create_file", path="src/pkg/big/mod.py", content="x" * 100_001)

        assert out.is_error and "at most 100000" in out.content
        assert snapshot(h.checkout) == before

    @pytest.mark.parametrize(
        "path",
        ["tests/test_new.py", "test_new.py", "tests/helpers.py", "conftest.py", "src/pkg/conftest.py", ".github/workflows/ci.yml"],
    )
    async def test_protected_paths_are_refused_and_no_directory_is_left_behind(self, h, path):
        before = snapshot(h.checkout)

        out = await h.call("create_file", path=path, content="x\n")

        assert out.is_error and ("read-only" in out.content or "already exists" in out.content or "off limits" in out.content)
        assert snapshot(h.checkout) == before

    @pytest.mark.parametrize("path", [".git/hooks/post-commit", ".git/config", ".gitattributes", ".gitmodules", ".GIT/hooks/post-commit"])
    async def test_dot_git_paths_are_refused_and_nothing_is_written(self, h, path):
        hooks_before = sorted(os.listdir(h.checkout / ".git/hooks"))
        before = snapshot(h.checkout)

        out = await h.call("create_file", path=path, content="#!/bin/sh\ntouch pwned\n")

        assert out.is_error
        assert snapshot(h.checkout) == before
        assert sorted(os.listdir(h.checkout / ".git/hooks")) == hooks_before

    @pytest.mark.parametrize("path", ["../escape.py", "/tmp/escape.py", "src/../../escape.py"])
    async def test_escapes_are_refused_and_nothing_is_written(self, h, tmp_path, path):
        out = await h.call("create_file", path=path, content="x\n")

        assert out.is_error
        assert not (tmp_path / "escape.py").exists()

    async def test_a_symlinked_parent_is_refused(self, h, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, h.checkout / "linkdir")

        out = await h.call("create_file", path="linkdir/new.py", content="x\n")

        assert out.is_error
        assert list(outside.iterdir()) == []

    async def test_a_parent_that_is_a_file_is_an_error(self, h):
        out = await h.call("create_file", path="src/pkg/util.py/inner.py", content="x\n")

        assert out.is_error and "is a file" in out.content

    async def test_a_nul_in_the_content_is_refused(self, h):
        before = snapshot(h.checkout)

        out = await h.call("create_file", path="src/pkg/n.py", content="a\x00b")

        assert out.is_error and "NUL" in out.content
        assert snapshot(h.checkout) == before

    async def test_content_that_cannot_be_encoded_is_refused(self, h):
        before = snapshot(h.checkout)

        out = await h.call("create_file", path="src/pkg/s.py", content="\ud800")

        assert out.is_error
        assert snapshot(h.checkout) == before

    async def test_a_path_inside_a_submodule_is_a_tool_error_not_a_crash(self, tmp_path):
        # A tracked gitlink (mode 160000) is an empty directory in a clone; `git check-ignore`
        # exits 128 for anything beneath it. The repository author controls that layout.
        h = make_harness(tmp_path)
        git(h.checkout, "update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},vendor_sub")
        git(h.checkout, "commit", "-q", "-m", "add a gitlink")
        (h.checkout / "vendor_sub").mkdir()
        before = snapshot(h.checkout)

        out = await h.call("create_file", path="vendor_sub/new.py", content="x = 1\n")

        assert out.is_error and out.content.startswith("cannot create files at that location")
        assert snapshot(h.checkout) == before

    async def test_a_checkout_git_cannot_read_is_a_tool_error_and_writes_nothing(self, tmp_path):
        h = make_harness(tmp_path)
        os.rename(h.checkout / ".git", tmp_path / "moved-git")
        before = snapshot(h.checkout)

        out = await h.call("create_file", path="src/pkg/new.py", content="x\n")

        assert out.is_error and "cannot create files at that location" in out.content
        assert snapshot(h.checkout) == before

    async def test_check_ignore_refuses_bare_repositories_it_might_discover(self, h, monkeypatch):
        from repolace_agents.tools import files as files_module

        seen = []
        real = files_module.run_git

        async def spy(*args, **kwargs):
            seen.append(args)
            return await real(*args, **kwargs)

        monkeypatch.setattr(files_module, "run_git", spy)

        await h.call("create_file", path="src/pkg/new.py", content="x\n")

        assert seen and seen[0][:2] == ("-c", "safe.bareRepository=explicit")


def test_make_checkout_is_a_real_repository(tmp_path):
    # Guards the fixture the tests above stand on: a plain directory would let
    # `git check-ignore` and `git grep` tests pass for the wrong reason.
    assert (make_checkout(tmp_path) / ".git").is_dir()
