"""`grep` and `search_code`.

`grep` runs `git grep` on the host with a pattern and a path the model chose, so
most of these tests are about what a hostile string cannot do: become an option,
become pathspec magic, become a tree-ish, or reach a file outside the tree.
"""

import os

import pytest

from repolace_agents.tools import search as search_module
from repolace_shared.git import GitTimeoutError

from tools_support import git, hit, make_checkout, make_harness, snapshot

pytestmark = pytest.mark.anyio

FILES = {
    "src/pkg/core.py": "def add(a, b):\n    return a + b  # needle\n",
    "src/pkg/util.py": "VALUE = 1\nNEEDLE = 2\n",
    "src/pkg/notes.txt": "a.b\naxb\nneedle in notes\n",
    "tests/test_core.py": "def test_add():\n    assert add(1, 2) == 3  # needle\n",
    "docs/readme.md": "a needle in the docs\n",
    "src/we:ird.py": "a:b needle\n",
    "src/ff.py": "x\x0cneedle y\n",
    ":(exclude)src": "needle in a file named like pathspec magic\n",
    "-h": "needle in a file named like an option\n",
    "flags.txt": "use -v to be verbose\n",
    ".github/workflows/ci.yml": "on: push  # needle in CI\n",
    ".gitattributes": "* text  # needle in attributes\n",
}


@pytest.fixture
def h(tmp_path):
    return make_harness(tmp_path, files=FILES)


def paths_in(content: str) -> list[str]:
    return [line.split(":", 1)[0] for line in content.splitlines() if not line.startswith("[")]


class TestGrep:
    async def test_it_prints_path_line_and_text(self, h):
        out = await h.call("grep", pattern="needle", path="src/pkg/core.py")

        assert out.content == "src/pkg/core.py:2:    return a + b  # needle"

    async def test_it_searches_the_working_tree_so_edits_are_visible(self, h):
        await h.call("edit_file", path="src/pkg/util.py", old_string="VALUE = 1", new_string="FRESH = 1")

        assert "src/pkg/util.py:1:FRESH = 1" in (await h.call("grep", pattern="FRESH")).content

    async def test_a_regex_is_posix_extended(self, h):
        out = await h.call("grep", pattern="a.b$", path="src/pkg/notes.txt")

        assert out.content.splitlines() == ["src/pkg/notes.txt:1:a.b", "src/pkg/notes.txt:2:axb"]

    async def test_fixed_string_treats_regex_characters_literally(self, h):
        out = await h.call("grep", pattern="a.b", path="src/pkg/notes.txt", fixed_string=True)

        assert out.content == "src/pkg/notes.txt:1:a.b"

    async def test_case_insensitive_matches_both_cases(self, h):
        sensitive = await h.call("grep", pattern="needle", path="src/pkg/util.py")
        insensitive = await h.call("grep", pattern="needle", path="src/pkg/util.py", case_insensitive=True)

        assert "no matches" in sensitive.content
        assert insensitive.content == "src/pkg/util.py:2:NEEDLE = 2"

    async def test_no_match_is_a_result_not_an_error(self, h):
        # `git grep` exits 1 for "no matches" and `run_git` raises on any non-zero exit.
        out = await h.call("grep", pattern="zzz_not_anywhere")

        assert not out.is_error
        assert "no matches for 'zzz_not_anywhere'" in out.content

    async def test_an_invalid_regex_is_an_error_the_model_can_read(self, h):
        out = await h.call("grep", pattern="[unclosed")

        assert out.is_error and out.content.startswith("grep failed:")

    async def test_a_pattern_that_is_invalid_as_a_regex_works_as_a_fixed_string(self, h):
        (h.checkout / "src/pkg/util.py").write_text("x = a[0\n")

        out = await h.call("grep", pattern="a[0", fixed_string=True, path="src/pkg/util.py")

        assert not out.is_error and "x = a[0" in out.content

    async def test_dot_git_is_never_searched(self, h):
        out = await h.call("grep", pattern="repositoryformatversion")

        assert "no matches" in out.content
        assert (await h.call("grep", pattern="needle", path=".git")).is_error

    async def test_path_narrows_to_a_directory_or_file(self, h):
        in_src = await h.call("grep", pattern="needle", path="src/pkg")

        assert sorted(set(paths_in(in_src.content))) == ["src/pkg/core.py", "src/pkg/notes.txt"]

    async def test_a_glob_filters_by_path(self, h):
        py = await h.call("grep", pattern="needle", glob="*.py")
        nested = await h.call("grep", pattern="needle", glob="tests/*.py")
        none = await h.call("grep", pattern="needle", glob="*.rs")

        assert "docs/readme.md" not in py.content and "src/pkg/core.py" in py.content
        assert paths_in(nested.content) == ["tests/test_core.py"]
        assert "no matches" in none.content and "'*.rs'" in none.content

    async def test_max_results_caps_the_matches_and_says_how_many_there_were(self, h):
        out = await h.call("grep", pattern="needle", max_results=2)

        lines = out.content.splitlines()
        assert len(lines) == 3 and lines[2].startswith("[showing the first 2 of ")

    async def test_the_default_cap_is_fifty(self, h):
        (h.checkout / "src/pkg/many.txt").write_text("hit\n" * 80)
        git(h.checkout, "add", "-A")

        out = await h.call("grep", pattern="hit", path="src/pkg/many.txt")

        assert len(out.content.splitlines()) == 51 and "first 50 of 80" in out.content

    async def test_a_result_count_above_the_limit_is_refused_by_the_schema(self, h):
        out = await h.call("grep", pattern="x", max_results=201)

        assert out.is_error and "at most 200" in out.content

    async def test_a_file_name_containing_a_colon_parses(self, h):
        out = await h.call("grep", pattern="needle", path="src/we:ird.py")

        assert out.content == "src/we:ird.py:1:a:b needle"

    async def test_a_form_feed_in_the_matched_text_does_not_split_the_line(self, h):
        out = await h.call("grep", pattern="needle", path="src/ff.py")

        assert out.content == "src/ff.py:1:x\x0cneedle y"

    async def test_a_very_long_match_line_is_clipped(self, h):
        (h.checkout / "src/pkg/min.js").write_text("needle" + "x" * 5000 + "\n")
        git(h.checkout, "add", "-A")

        out = await h.call("grep", pattern="needle", path="src/pkg/min.js")

        assert len(out.content) < 400 and out.content.endswith("...")

    async def test_only_tracked_files_are_searched(self, h):
        await h.call("create_file", path="src/pkg/fresh.py", content="UNTRACKED_MARKER = 1\n")

        assert "no matches" in (await h.call("grep", pattern="UNTRACKED_MARKER")).content
        assert "TRACKED files only" in h.box.schemas()[2]["function"]["description"]

    async def test_it_times_out_with_a_message_to_narrow_the_search(self, h, monkeypatch):
        async def slow(*args, **kwargs):
            raise GitTimeoutError(args, 20.0)

        monkeypatch.setattr(search_module, "run_limited_git", slow)

        out = await h.call("grep", pattern="needle")

        assert out.is_error and "narrow it" in out.content


class TestGrepResourceLimits:
    """glibc's regex engine is not safe against a hostile pattern: only `-F` is."""

    MEMORY_BOMB = "((a{1,200}){1,200}){1,200}b"  # 28 characters; ~5 GB resident under plain git
    BACKREFERENCES = r"(.*)(.*)(.*)(.*)(.*)(.*)(.*)(.*)\1\2\3\4\5\6\7\8x"  # exponential in CPU

    @pytest.mark.parametrize("pattern", [MEMORY_BOMB, BACKREFERENCES])
    async def test_the_reviewers_patterns_are_refused_up_front(self, h, pattern):
        out = await h.call("grep", pattern=pattern)

        assert out.is_error and "fixed_string=true" in out.content

    @pytest.mark.parametrize("pattern", ["(a)\\1", "x{100}", "x{1,100}", "x{100,}", "x{,150}", "(x{2}){3}{500}", "a{99999999999999999999}"])
    async def test_a_backreference_or_a_bound_of_a_hundred_is_refused_in_a_regex(self, h, pattern):
        out = await h.call("grep", pattern=pattern)

        assert out.is_error and "fixed_string=true" in out.content

    @pytest.mark.parametrize("pattern", ["x{99}", "x{1,99}", "(a|b){2}", "x{0}", "needle", r"a\.b", "[0-9]{1,3}"])
    async def test_ordinary_patterns_and_small_bounds_still_run(self, h, pattern):
        assert not (await h.call("grep", pattern=pattern)).is_error

    async def test_the_same_text_is_fine_as_a_fixed_string(self, h):
        for pattern in (self.MEMORY_BOMB, self.BACKREFERENCES, "x{100}"):
            out = await h.call("grep", pattern=pattern, fixed_string=True)

            assert not out.is_error and "no matches" in out.content

    async def test_a_pattern_that_gets_past_the_check_still_dies_at_the_memory_limit(self, h, monkeypatch):
        # ((a{1,99}){1,99}){1,99}b is under the bound check but still explodes. Run it with a
        # small limit so the test bounds itself instead of eating the real 512 MiB.
        monkeypatch.setattr(search_module, "GREP_MAX_MEMORY_BYTES", 128 * 1024 * 1024)

        out = await h.call("grep", pattern="((a{1,99}){1,99}){1,99}b")

        assert out.is_error and "too much memory or CPU time" in out.content and "fixed_string=true" in out.content

    async def test_the_child_is_single_threaded_and_the_path_stays_after_the_double_dash(self, h, monkeypatch):
        """A shape test, deliberately: it pins the argv, not an observed behaviour.

        No cheap behavioural check exists for "git ran without worker threads", and the property
        that matters (the pattern is the operand of `-e`, the path comes after `--`) is covered by
        behaviour in `TestGrepInjection`; this one only keeps `--threads=1` and the limits from
        being dropped silently.
        """
        seen = []
        real = search_module.run_limited_git

        async def spy(*args, **kwargs):
            seen.append((args, kwargs))
            return await real(*args, **kwargs)

        monkeypatch.setattr(search_module, "run_limited_git", spy)

        await h.call("grep", pattern="needle", path="src/pkg")

        (argv, kwargs), = seen
        assert argv[:3] == ("--literal-pathspecs", "grep", "--threads=1")
        assert argv[-4:] == ("-e", "needle", "--", "src/pkg")
        assert kwargs["max_memory_bytes"] == 512 * 1024 * 1024 and kwargs["max_cpu_seconds"] == 15

    async def test_a_signal_death_is_reported_as_a_resource_stop(self, h, monkeypatch):
        from repolace_agents.tools.gitproc import LimitedGitResult

        async def killed(*args, **kwargs):
            return LimitedGitResult(returncode=-24, stdout=b"", stderr=b"", output_cut=False)

        monkeypatch.setattr(search_module, "run_limited_git", killed)

        out = await h.call("grep", pattern="needle")

        assert out.is_error and "too much memory or CPU time" in out.content

    async def test_output_beyond_the_cap_is_cut_and_says_so(self, h, monkeypatch):
        (h.checkout / "src/pkg/lots.txt").write_text("hit line of text\n" * 5000)
        git(h.checkout, "add", "-A")
        monkeypatch.setattr(search_module, "GREP_MAX_OUTPUT_BYTES", 2000)

        out = await h.call("grep", pattern="hit", path="src/pkg/lots.txt", max_results=200)

        lines = out.content.splitlines()
        assert lines[-1].startswith("[grep output was cut at 2000 bytes")
        assert 0 < len(lines) - 1 < 200
        # No half-record: the cut landed mid-line, and the line it landed in is dropped.
        assert all(line.endswith(":hit line of text") for line in lines[:-1])

    async def test_a_timeout_still_reports_narrowing(self, h, monkeypatch):
        # `run_limited_git` raising GitTimeoutError is covered above; this holds that a real
        # expiry (not a stub) maps the same way.
        monkeypatch.setattr(search_module, "GREP_TIMEOUT_SECONDS", 0.0001)

        out = await h.call("grep", pattern="needle")

        assert out.is_error and "narrow it" in out.content


class TestGrepNeverReportsNoMatchesWhenItWasCut:
    """A cut search that found nothing it could show is INCOMPLETE, not empty."""

    async def test_a_first_record_larger_than_the_cap_does_not_hide_later_matches(self, tmp_path, monkeypatch):
        # A minified bundle sorts first and is one line over the cap; its record is dropped as a
        # half-record, leaving no output at all while `zzz_real.py` really does match.
        files = {
            "aaa_bundle.min.js": "x" * 5000 + " needle " + "y" * 100 + "\n",
            "zzz_real.py": "needle = 1\n",
        }
        h = make_harness(tmp_path, files=files)
        monkeypatch.setattr(search_module, "GREP_MAX_OUTPUT_BYTES", 2000)

        out = await h.call("grep", pattern="needle", fixed_string=True)

        assert out.content.startswith("INCOMPLETE") and "does NOT mean" in out.content
        assert "no matches for" not in out.content and "path" in out.content
        narrowed = await h.call("grep", pattern="needle", fixed_string=True, path="zzz_real.py")
        assert narrowed.content == "zzz_real.py:1:needle = 1"

    async def test_a_glob_applied_after_the_cut_does_not_turn_it_into_no_matches(self, tmp_path, monkeypatch):
        files = {f"aaa_docs/page_{n}.md": "needle in the docs\n" * 60 for n in range(3)}
        files["src/real.py"] = "needle = 1\n"
        h = make_harness(tmp_path, files=files)
        monkeypatch.setattr(search_module, "GREP_MAX_OUTPUT_BYTES", 2000)

        out = await h.call("grep", pattern="needle", fixed_string=True, glob="*.py")

        assert out.content.startswith("INCOMPLETE") and "matching glob '*.py'" in out.content
        assert "no matches for" not in out.content
        assert (await h.call("grep", pattern="needle", fixed_string=True, path="src")).content == "src/real.py:1:needle = 1"

    async def test_a_search_that_was_not_cut_still_says_no_matches(self, h):
        out = await h.call("grep", pattern="zzz_not_anywhere")

        assert out.content.startswith("no matches for") and "INCOMPLETE" not in out.content


class TestGrepFailureMessages:
    @pytest.mark.parametrize(
        "stderr",
        [
            b"fatal: -e option, 'x': Memory exhausted\n",
            b"fatal: Out of memory, malloc failed (tried to allocate 600000001 bytes)\n",
            b"fatal: out of memory\n",
        ],
    )
    async def test_git_reporting_out_of_memory_gets_the_same_advice_as_a_kill(self, h, monkeypatch, stderr):
        from repolace_agents.tools.gitproc import LimitedGitResult

        async def oom(*args, **kwargs):
            return LimitedGitResult(returncode=128, stdout=b"", stderr=stderr, output_cut=False)

        monkeypatch.setattr(search_module, "run_limited_git", oom)

        out = await h.call("grep", pattern="needle")

        assert out.is_error and "too much memory or CPU time" in out.content and "fixed_string=true" in out.content
        assert "fatal" not in out.content

    async def test_a_host_path_in_git_stderr_is_scrubbed(self, h, monkeypatch):
        from repolace_agents.tools.gitproc import LimitedGitResult

        async def dubious(*args, **kwargs):
            message = f"fatal: detected dubious ownership in repository at '{h.checkout}'\n"
            return LimitedGitResult(returncode=128, stdout=b"", stderr=message.encode(), output_cut=False)

        monkeypatch.setattr(search_module, "run_limited_git", dubious)

        out = await h.call("grep", pattern="needle")

        assert out.is_error and str(h.checkout) not in out.content and str(h.checkout.parent) not in out.content

    async def test_the_description_says_which_regex_dialect_this_is(self, h):
        description = h.box.schemas()[2]["function"]["description"]

        assert "\\d, \\w and \\s do not work" in description and "[0-9]" in description


class TestGrepOutputIsHonest:
    async def test_a_file_name_with_a_newline_cannot_forge_a_record(self, tmp_path):
        files = dict(FILES)
        files["src/pkg/core.py"] = "def add(a, b):\n    return a + b\n"  # no needle here
        files["forge/x\nsrc/pkg/core.py"] = "NEEDLE forged\n"
        h = make_harness(tmp_path, files=files)

        out = await h.call("grep", pattern="NEEDLE", fixed_string=True, path="forge")

        assert not any(line.startswith("src/pkg/core.py:") for line in out.content.splitlines())
        assert out.content == "forge/x\\x0asrc/pkg/core.py:1:NEEDLE forged"

    async def test_a_file_name_with_control_characters_is_shown_escaped(self, tmp_path):
        files = dict(FILES)
        files["nl\nname.py"] = "NEEDLE here\n"
        files["esc\x1b[31mred.py"] = "NEEDLE there\n"
        h = make_harness(tmp_path, files=files)

        out = await h.call("grep", pattern="NEEDLE", fixed_string=True)

        assert "nl\\x0aname.py:1:NEEDLE here" in out.content
        assert "esc\\x1b[31mred.py:1:NEEDLE there" in out.content
        assert "\x1b" not in out.content

    async def test_a_matched_nul_late_in_a_file_cannot_start_a_forged_record(self, tmp_path):
        # `-I` only looks at the first 8000 bytes, so a NUL later in the file reaches the output.
        files = dict(FILES)
        files["late.txt"] = "x" * 9000 + "\nneedle\0src/pkg/core.py\0 7\0fake\n"
        h = make_harness(tmp_path, files=files)

        out = await h.call("grep", pattern="needle", path="late.txt")

        assert not any(line.startswith("src/pkg/core.py:") for line in out.content.splitlines())

    async def test_tracked_dot_git_family_files_are_not_searched_for_the_model(self, h):
        # read_file and list_dir refuse them; grep must not be the way around that.
        out = await h.call("grep", pattern="needle")

        assert out.content.count("\n") >= 1
        assert ".github" not in out.content and ".gitattributes" not in out.content
        assert "no matches" in (await h.call("grep", pattern="needle in CI", path=".")).content

    async def test_a_glob_cannot_resurrect_a_git_family_path(self, h):
        assert "no matches" in (await h.call("grep", pattern="needle", glob="*.yml")).content


class TestGrepInjection:
    """A model-supplied string is never before `--` and never an option."""

    async def test_a_pattern_that_is_a_git_option_is_just_text(self, h, tmp_path):
        marker = tmp_path / "pwned"

        out = await h.call("grep", pattern=f"--open-files-in-pager=touch {marker}", fixed_string=True)

        assert not out.is_error and "no matches" in out.content
        assert not marker.exists()

    @pytest.mark.parametrize("pattern", ["-v", "--help", "-e", "--", "-f /etc/passwd", "--no-index"])
    async def test_a_pattern_starting_with_a_dash_is_a_pattern(self, h, pattern):
        out = await h.call("grep", pattern=pattern, fixed_string=True, path="flags.txt")

        assert not out.is_error
        assert "usage:" not in out.content

    async def test_a_dash_pattern_actually_matches_the_text(self, h):
        out = await h.call("grep", pattern="-v", fixed_string=True)

        assert out.content == "flags.txt:1:use -v to be verbose"

    async def test_a_path_named_like_a_flag_is_a_path(self, h):
        out = await h.call("grep", pattern="needle", path="-h")

        assert out.content == "-h:1:needle in a file named like an option"

    async def test_a_path_named_like_pathspec_magic_is_literal(self, h):
        # Without --literal-pathspecs git would read this as "everything except src".
        out = await h.call("grep", pattern="needle", path=":(exclude)src")

        assert out.content == ":(exclude)src:1:needle in a file named like pathspec magic"

    async def test_a_path_that_looks_like_a_revision_is_not_one(self, h):
        # `origin/main` and `HEAD:src` are tree-ishes if they reach git before `--`.
        for path in ("origin/main", "HEAD:src/pkg/core.py", "main"):
            out = await h.call("grep", pattern="needle", path=path)
            assert out.is_error and "does not exist" in out.content

    async def test_a_ref_named_like_a_directory_does_not_change_what_is_searched(self, h):
        git(h.checkout, "branch", "docs")  # a ref and a directory with one name

        out = await h.call("grep", pattern="needle", path="docs")

        assert out.content == "docs/readme.md:1:a needle in the docs"

    @pytest.mark.parametrize("pattern", ["a\x00b", "needle\nzzz", "x\r\n-f"])
    async def test_nul_and_newlines_in_the_pattern_are_refused(self, h, pattern):
        out = await h.call("grep", pattern=pattern)

        assert out.is_error and "single line" in out.content

    async def test_a_lone_surrogate_in_the_pattern_is_refused_not_a_crash(self, h):
        # JSON allows "\ud800"; passing it to a subprocess raises UnicodeEncodeError.
        out = await h.call("grep", pattern="\ud800")

        assert out.is_error and "not valid text" in out.content

    async def test_a_huge_pattern_is_refused_by_the_schema(self, h):
        out = await h.call("grep", pattern="a" * 10_000)

        assert out.is_error and "at most 200" in out.content

    @pytest.mark.parametrize("path", ["a\x00b", "../", "/etc", ".git/hooks", "", "x" * 600])
    async def test_a_malformed_or_escaping_path_is_refused(self, h, path):
        assert (await h.call("grep", pattern="needle", path=path)).is_error

    async def test_a_redos_pattern_is_not_a_python_regex(self, h):
        # POSIX ERE through git: nothing here is evaluated by Python's backtracking `re`.
        out = await h.call("grep", pattern="(a+)+$", path="src/pkg/notes.txt")

        assert not out.is_error

    async def test_a_tracked_symlink_to_a_host_file_is_not_followed(self, tmp_path):
        host = tmp_path / "host"
        host.mkdir()
        (host / "secret.txt").write_text("SECRET_TOKEN=hunter2\n")
        h = make_harness(tmp_path, files=FILES)
        os.symlink(host / "secret.txt", h.checkout / "src/leak.py")
        git(h.checkout, "add", "-A")
        git(h.checkout, "commit", "-q", "-m", "add symlink")

        out = await h.call("grep", pattern="SECRET_TOKEN")

        assert "hunter2" not in out.content
        assert (await h.call("grep", pattern="SECRET", path="src/leak.py")).is_error

    async def test_a_glob_is_never_given_to_git(self, h, tmp_path):
        marker = tmp_path / "pwned"

        out = await h.call("grep", pattern="needle", glob=f"--open-files-in-pager=touch {marker}")

        assert not out.is_error and not marker.exists()


class TestSearchCode:
    async def test_it_formats_each_hit_with_its_location_and_snippet(self, tmp_path):
        h = make_harness(tmp_path, hits=[hit(), hit("src/pkg/util.py", 3, 9, "Util.run", chunk_type="method")])

        out = await h.call("search_code", query="adding numbers")

        assert out.content.split("\n\n")[0] == (
            "src/pkg/core.py:1-2 add (function)\n    def add(a, b):\n        return a + b"
        )
        assert "src/pkg/util.py:3-9 Util.run (method)" in out.content

    async def test_the_default_limit_is_eight_and_the_query_is_passed_through(self, tmp_path):
        h = make_harness(tmp_path, hits=[hit()])

        await h.call("search_code", query="adding numbers")
        await h.call("search_code", query="x", limit=3)

        assert h.searches == [("adding numbers", 8), ("x", 3)]

    async def test_more_hits_than_the_limit_are_cut(self, tmp_path):
        h = make_harness(tmp_path, hits=[hit(f"f{i}.py") for i in range(10)])

        out = await h.call("search_code", query="x", limit=2)

        assert out.content.count("(function)") == 2

    async def test_a_snippet_is_cut_to_thirty_lines_of_bounded_width(self, tmp_path):
        snippet = "\n".join(["y" * 500] + [f"line {i}" for i in range(100)])
        h = make_harness(tmp_path, hits=[hit(snippet=snippet)])

        out = await h.call("search_code", query="x")

        body = out.content.splitlines()[1:]
        assert len(body) == 30 and len(body[0]) <= 4 + 200 + 3

    async def test_a_lone_surrogate_in_the_query_is_refused_before_the_index_sees_it(self, tmp_path):
        h = make_harness(tmp_path, hits=[hit()])

        out = await h.call("search_code", query="\ud800")

        assert out.is_error and "not valid text" in out.content
        assert h.searches == []

    @pytest.mark.parametrize("query", [" ", "   \t\n ", "\x00", "foo\x00bar", "\n"])
    async def test_a_blank_query_or_one_with_a_nul_is_refused_before_the_index_is_called(self, tmp_path, query):
        # Real retrieval raises ValueError on a blank query and Postgres rejects a NUL.
        h = make_harness(tmp_path, hits=[hit()])

        out = await h.call("search_code", query=query)

        assert out.is_error and out.content == "query must contain visible text"
        assert h.searches == []

    async def test_no_hits_says_so_and_points_at_grep(self, tmp_path):
        out = await make_harness(tmp_path).call("search_code", query="x")

        assert not out.is_error and "no matches" in out.content and "grep" in out.content

    @pytest.mark.parametrize(("kwargs", "message"), [({"query": "q" * 501}, "at most 500"), ({"query": ""}, "at least 1"), ({"query": "x", "limit": 21}, "at most 20"), ({"query": "x", "limit": 0}, "at least 1")])
    async def test_the_schema_bounds_hold(self, tmp_path, kwargs, message):
        h = make_harness(tmp_path, hits=[hit()])

        out = await h.call("search_code", **kwargs)

        assert out.is_error and message in out.content
        assert h.searches == []

    async def test_the_description_says_the_index_is_the_base_commit(self, tmp_path):
        spec = make_harness(tmp_path).box.schemas()[0]["function"]

        assert spec["name"] == "search_code"
        assert "base commit" in spec["description"] and "read_file" in spec["description"]


def test_snapshot_ignores_dot_git(tmp_path):
    checkout = make_checkout(tmp_path)

    assert not any(path.startswith(".git/") or path == ".git" for path in snapshot(checkout))
