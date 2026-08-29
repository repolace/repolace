"""Unit tests for the deterministic stub editor.

Rendering is pure, so these assert on real output rather than on mocks. The
applying half runs against a real file tree via `tmp_path`, matching the
real-fixtures-over-mocks style the rest of this repo uses.

The property worth defending hardest is that the marker names the retrieval hit
it came from. A stub that wrote to a fixed path would pass an end-to-end run
with retrieval entirely broken, which would make the whole slice worthless.
"""

import ast

import pytest

from repolace_pipeline.edit import (
    NOT_A_FIX,
    apply_stub_edit,
    commit_message,
    pr_title,
    render_marker,
    render_pr_body,
)

from pipeline_support import TASK_ID, chunk, empty_request, request

SOURCE = '''"""Module docstring."""

def parse_config(path):
    return path


class Loader:
    def load(self, name):
        return name
'''


class TestRenderMarker:
    def test_says_it_is_not_a_fix_first(self):
        first = render_marker(request()).splitlines()[0]

        assert NOT_A_FIX in first

    def test_names_the_retrieval_hit_that_selected_the_file(self):
        marker = render_marker(request(chunk(file_path="src/config.py", symbol_name="load_cfg")))

        assert "src/config.py:3-9" in marker
        assert "load_cfg" in marker
        assert "rrf=0.0328" in marker
        assert "semantic=1" in marker

    def test_an_arm_that_did_not_return_the_chunk_shows_as_absent(self):
        """`None` is not rank 0 -- it means that arm never returned this chunk."""
        marker = render_marker(request(chunk(keyword_rank=None)))

        assert "keyword=-" in marker
        assert "keyword=None" not in marker

    def test_carries_task_issue_and_base_commit(self):
        marker = render_marker(request())

        assert TASK_ID.hex in marker
        assert "#7" in marker
        assert "a1b2c3d4e5f6a7b8c9d0" in marker

    def test_every_line_is_a_comment(self):
        """Anything else would be a syntax error in the file it is inserted into."""
        assert all(line.lstrip().startswith("#") for line in render_marker(request()).splitlines())

    def test_is_deterministic(self):
        assert render_marker(request()) == render_marker(request())

    def test_indent_is_applied_to_every_line(self):
        marker = render_marker(request(), indent="    ")

        assert all(line.startswith("    #") for line in marker.splitlines())

    def test_a_qualified_method_shows_its_class(self):
        marker = render_marker(request(chunk(class_name="Loader", symbol_name="load")))

        assert "Loader.load" in marker

    def test_no_retrieval_is_a_programming_error_not_a_blank_marker(self):
        """The editor cannot invent a target. run_task guards this earlier too."""
        with pytest.raises(ValueError, match="at least one retrieved chunk"):
            render_marker(empty_request())


class TestApplyStubEdit:
    def write_source(self, tmp_path, text: str = SOURCE):
        path = tmp_path / "src" / "app.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_inserts_above_the_retrieved_start_line(self, tmp_path):
        self.write_source(tmp_path)

        edited = apply_stub_edit(tmp_path, request(chunk(start_line=3)))

        lines = edited.read_text().splitlines()
        assert NOT_A_FIX in lines[2], "marker should sit where the chunk started"
        assert lines[-1].strip() == "return name", "the rest of the file is untouched"

    def test_the_result_is_still_valid_python(self, tmp_path):
        """The whole point of inserting a comment rather than code."""
        self.write_source(tmp_path)

        edited = apply_stub_edit(tmp_path, request(chunk(start_line=3)))

        ast.parse(edited.read_text())

    def test_an_indented_method_stays_valid_and_keeps_its_indent(self, tmp_path):
        self.write_source(tmp_path)

        edited = apply_stub_edit(
            tmp_path, request(chunk(start_line=8, class_name="Loader", symbol_name="load"))
        )

        text = edited.read_text()
        ast.parse(text)
        assert "    # " in text

    def test_the_original_content_survives(self, tmp_path):
        self.write_source(tmp_path)

        edited = apply_stub_edit(tmp_path, request())

        text = edited.read_text()
        for line in SOURCE.splitlines():
            assert line in text

    def test_a_start_line_past_the_end_of_the_file_appends(self, tmp_path):
        """The index can outlive an edit that shortened the file."""
        self.write_source(tmp_path, "x = 1\n")

        edited = apply_stub_edit(tmp_path, request(chunk(start_line=999)))

        ast.parse(edited.read_text())
        assert NOT_A_FIX in edited.read_text()

    def test_a_missing_file_fails_loudly(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="not a file in this checkout"):
            apply_stub_edit(tmp_path, request(chunk(file_path="src/gone.py")))

    def test_returns_the_path_it_edited(self, tmp_path):
        expected = self.write_source(tmp_path)

        assert apply_stub_edit(tmp_path, request()) == expected


class TestPullRequestText:
    def test_title_names_the_issue_and_flags_the_smoke_test(self):
        title = pr_title(request())

        assert "#7" in title
        assert "repolace" in title

    def test_body_lists_every_retrieved_chunk(self):
        body = render_pr_body(request(chunk(file_path="a.py"), chunk(file_path="b.py")))

        assert "a.py" in body and "b.py" in body

    def test_body_never_closes_the_issue(self):
        """Merging must not close an issue that was never fixed."""
        body = render_pr_body(request()).lower()

        for keyword in ("closes #", "fixes #", "resolves #"):
            assert keyword not in body

    def test_body_says_it_is_not_a_fix_and_asks_for_a_close(self):
        body = render_pr_body(request())

        assert NOT_A_FIX in body
        assert "close this pull request" in body.lower()

    def test_body_marks_empty_retrieval_explicitly(self):
        body = render_pr_body(empty_request())

        assert "No chunks retrieved" in body

    def test_commit_message_says_not_a_fix(self):
        assert "Not a fix" in commit_message(request())


class TestApplyStubEditConfinement:
    """`file_path` is untrusted input to a filesystem write.

    Today it comes from a `code_chunks` row, which the indexer wrote -- so the
    stub cannot actually be steered. These tests are for the shape of the
    function, because the real Editor writes model-chosen paths, and at that
    point `repo_path / file_path` becomes a one-string traversal into `.git`.
    """

    def write_source(self, tmp_path):
        path = tmp_path / "src" / "app.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SOURCE)
        return path

    def test_an_absolute_retrieved_path_is_refused(self, tmp_path):
        """pathlib discards the left operand entirely for an absolute right
        one, so `repo_path / "/etc/passwd"` *is* `/etc/passwd`. No `..`, nothing
        in the string that looks wrong."""
        self.write_source(tmp_path)
        victim = tmp_path / "victim.py"
        victim.write_text("UNTOUCHED = True\n")

        with pytest.raises(ValueError, match="not usable in this checkout"):
            apply_stub_edit(tmp_path / "repo", request(chunk(file_path=str(victim))))

        assert victim.read_text() == "UNTOUCHED = True\n"

    def test_a_retrieved_path_that_climbs_out_is_refused(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_text(SOURCE)
        victim = tmp_path / "victim.py"
        victim.write_text("UNTOUCHED = True\n")

        with pytest.raises(ValueError, match="not usable in this checkout"):
            apply_stub_edit(repo, request(chunk(file_path="../victim.py")))

        assert victim.read_text() == "UNTOUCHED = True\n"

    def test_the_editor_will_not_write_through_a_symlink(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        victim = tmp_path / "victim.py"
        victim.write_text("UNTOUCHED = True\n")
        (repo / "src" / "app.py").symlink_to(victim)

        with pytest.raises(ValueError, match="not usable in this checkout"):
            apply_stub_edit(repo, request(chunk(file_path="src/app.py")))

        assert victim.read_text() == "UNTOUCHED = True\n"

    def test_the_editor_refuses_to_write_into_dot_git(self, tmp_path):
        """The one that becomes host code execution: a post-commit hook fires on
        the very next `record_attempt`, as the worker user, with no sandbox
        involved at any point."""
        repo = tmp_path / "repo"
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "post-commit").write_text("#!/bin/sh\n")

        with pytest.raises(ValueError, match="refusing to edit inside .git"):
            apply_stub_edit(repo, request(chunk(file_path=".git/hooks/post-commit")))

        assert (hooks / "post-commit").read_text() == "#!/bin/sh\n"
