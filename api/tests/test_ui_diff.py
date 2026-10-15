"""The diff parser and its highlighter. The diff is attacker-influenced text: everything is escaped."""

import re
from html import unescape

from markupsafe import Markup
from repolace_api.ui.diffview import MAX_LINES, highlight_line, parse_diff
from repolace_api.ui.router import env

SIMPLE = """\
diff --git a/pkg/app.py b/pkg/app.py
index 111..222 100644
--- a/pkg/app.py
+++ b/pkg/app.py
@@ -1,4 +1,4 @@ def f():
 import os
-x = 1  # old
+x = "new"  # new
 def g():
-    return 1
+    return 2
"""


def render_diff(parsed) -> str:
    return env.from_string('{% from "_macros.html" import diff_block %}{{ diff_block(d, true, true) }}').render(d=parsed)


class TestParse:
    def test_files_stats_numbers_and_signs(self):
        parsed = parse_diff(SIMPLE)

        assert [f.path for f in parsed.files] == ["pkg/app.py"]
        assert (parsed.added, parsed.deleted) == (2, 2)
        lines = parsed.files[0].lines
        assert [ln.kind for ln in lines] == ["hunk", "ctx", "del", "add", "ctx", "del", "add"]
        assert (lines[1].old, lines[1].new) == (1, 1)
        assert (lines[2].old, lines[2].new) == (2, None)
        assert (lines[3].old, lines[3].new) == (None, 2)
        assert [ln.sign for ln in lines[1:]] == [" ", "-", "+", " ", "-", "+"]

    def test_added_deleted_and_renamed_files_are_tagged(self):
        parsed = parse_diff(
            "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+a\n"
            "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
            "diff --git a/m.py b/n.py\nsimilarity index 100%\nrename from m.py\nrename to n.py\n"
        )
        assert [(f.path, f.status) for f in parsed.files] == [("new.py", "added"), ("old.py", "deleted"), ("n.py", "renamed")]

    def test_a_removed_line_that_looks_like_a_file_header_stays_a_removed_line(self):
        # `-- foo` removed gives the line `--- foo`; the hunk counts say it is body, not a header.
        parsed = parse_diff("diff --git a/a.sql b/a.sql\n--- a/a.sql\n+++ b/a.sql\n@@ -1,2 +1,1 @@\n--- foo\n keep\n")
        kinds = [ln.kind for ln in parsed.files[0].lines]
        assert len(parsed.files) == 1
        assert kinds == ["hunk", "del", "ctx"]

    def test_a_bare_patch_without_git_headers_parses(self):
        parsed = parse_diff("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-c\n+d\n")
        assert [f.path for f in parsed.files] == ["x.py", "y.py"]

    def test_no_newline_marker_and_binary_files_are_meta_lines(self):
        parsed = parse_diff(
            "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-x\n\\ No newline at end of file\n+y\n"
            "diff --git a/i.png b/i.png\nBinary files a/i.png and b/i.png differ\n"
        )
        assert [ln.kind for ln in parsed.files[0].lines] == ["hunk", "del", "meta", "add"]
        assert parsed.files[1].binary

    def test_garbage_does_not_raise(self):
        for text in ("", "\n\n", "not a diff at all", "@@ -1 +1 @@\n+x", "diff --git\n@@ bad @@\n+++", "\x00\x01"):
            parse_diff(text)

    def test_a_large_diff_is_capped_and_says_how_much_was_dropped(self):
        body = "".join(f"+line {i}\n" for i in range(MAX_LINES + 50))
        parsed = parse_diff("diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1,3000 @@\n" + body)
        assert parsed.truncated_lines > 0
        assert "Diff truncated" in render_diff(parsed)


class TestHighlight:
    def test_python_tokens_get_classes_and_text_survives(self):
        html = highlight_line('def f(): return "s"  # c', "x.py")
        assert isinstance(html, Markup)
        assert '<span class="k">def</span>' in html
        assert '<span class="s">' in html and '<span class="cm"># c</span>' in html
        assert unescape(re.sub(r"<[^>]+>", "", str(html))) == 'def f(): return "s"  # c'

    def test_an_unknown_extension_is_plain_escaped_text(self):
        assert highlight_line("a < b", "notes.zzz") == Markup("a &lt; b")

    def test_a_very_long_line_is_not_lexed(self):
        html = highlight_line("x" * 5000, "x.py")
        assert "<span" not in html


class TestEscaping:
    PAYLOAD = '</pre><script>alert(1)</script><img src=x onerror=alert(2)>'

    def diff_with(self, line: str, path: str = "a.py") -> str:
        return (
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+{line}\n"
        )

    def test_a_script_and_pre_payload_in_added_removed_and_context_lines_is_inert(self):
        for marker in ("+", "-", " "):
            body = f"{marker}{self.PAYLOAD}\n"
            counts = {"+": "@@ -0,0 +1 @@", "-": "@@ -1 +0,0 @@", " ": "@@ -1 +1 @@"}[marker]
            html = render_diff(parse_diff(f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n{counts}\n{body}"))
            assert "<script" not in html and "</pre>" not in html and "<img" not in html
            assert "&lt;script&gt;" in html

    def test_a_payload_in_a_file_name_a_hunk_header_and_a_meta_line_is_inert(self):
        name = "<script>alert(1)</script>.py"
        text = (
            f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@ </pre><script>x</script>\n-a\n+b\n"
            "diff --git a/z b/z\nBinary files </pre><script>y</script> differ\n"
        )
        html = render_diff(parse_diff(text))
        assert "<script" not in html and "</pre>" not in html
        assert 'aria-label="Diff of &lt;script&gt;' in html  # attribute context is escaped too

    def test_a_payload_inside_a_python_string_token_cannot_break_out_of_the_span(self):
        html = str(highlight_line('x = "</span><script>alert(1)</script>"', "a.py"))
        assert "<script" not in html and "</span><script" not in html

    def test_a_quote_cannot_escape_the_aria_label_attribute(self):
        html = render_diff(parse_diff(self.diff_with("b", path='x" onfocus="alert(1).py')))
        assert 'onfocus="alert' not in html

    def test_the_template_prints_the_markup_without_a_safe_filter(self):
        from pathlib import Path

        macros = (Path(env.loader.searchpath[0]) / "_macros.html").read_text()  # type: ignore[union-attr]
        assert "|safe" not in macros and "| safe" not in macros
