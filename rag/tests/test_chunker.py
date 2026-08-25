"""Unit tests for tree-sitter AST chunking.

Pure parsing — no database, no embedding model.
"""

import textwrap

from retrieval.chunker import chunk_python_file


def chunks_for(source: str):
    return chunk_python_file("sample.py", textwrap.dedent(source).lstrip("\n"))


def by_symbol(source: str) -> dict:
    return {c.symbol_name: c for c in chunks_for(source)}


class TestTopLevelFunctions:
    def test_captures_a_function_with_1_based_line_span(self):
        (c,) = chunks_for(
            """
            def retry(attempts):
                return attempts > 0
            """
        )

        assert c.chunk_type == "function"
        assert c.symbol_name == "retry"
        assert c.class_name is None
        assert c.file_path == "sample.py"
        assert (c.start_line, c.end_line) == (1, 2)
        assert "return attempts > 0" in c.content

    def test_async_functions_are_captured(self):
        (c,) = chunks_for(
            """
            async def fetch(url):
                return url
            """
        )

        assert c.chunk_type == "function"
        assert c.symbol_name == "fetch"

    def test_decorated_function_span_starts_at_the_decorator(self):
        (c,) = chunks_for(
            """
            @cache
            def expensive():
                return 1
            """
        )

        assert c.symbol_name == "expensive"
        assert c.start_line == 1, "span should include the decorator line"
        assert c.content.startswith("@cache")

    def test_multiple_top_level_functions_are_separate_chunks(self):
        result = by_symbol(
            """
            def one():
                return 1

            def two():
                return 2
            """
        )

        assert set(result) == {"one", "two"}


class TestClasses:
    SOURCE = """
        class Worker(Base):
            \"\"\"Does the work.\"\"\"

            def run(self, task):
                prepared = self.prepare(task)
                return prepared

            def prepare(self, task):
                return task
        """

    def test_emits_a_skeleton_plus_a_chunk_per_long_method(self):
        result = by_symbol(self.SOURCE)

        assert result["Worker"].chunk_type == "class_skeleton"
        assert result["run"].chunk_type == "method"
        assert result["prepare"].chunk_type == "method"

    def test_methods_carry_their_class_name(self):
        result = by_symbol(self.SOURCE)

        assert result["run"].class_name == "Worker"
        assert result["Worker"].class_name == "Worker"

    def test_skeleton_spans_the_whole_class(self):
        result = by_symbol(self.SOURCE)
        skeleton = result["Worker"]

        assert skeleton.start_line == 1
        assert skeleton.end_line >= result["prepare"].end_line

    def test_skeleton_lists_method_signatures_without_their_bodies(self):
        skeleton = by_symbol(self.SOURCE)["Worker"]

        assert "def run(self, task)" in skeleton.content
        assert "def prepare(self, task)" in skeleton.content
        assert "prepared = self.prepare(task)" not in skeleton.content

    def test_skeleton_keeps_a_method_docstring(self):
        skeleton = by_symbol(
            """
            class Worker:
                def run(self):
                    \"\"\"Run it.\"\"\"
                    return 1
            """
        )["Worker"]

        assert "Run it." in skeleton.content

    def test_short_methods_fold_into_the_skeleton(self):
        """MIN_STANDALONE_CHUNK_LINES: a one-line body embeds poorly alone."""
        result = by_symbol(
            """
            class Point:
                def x(self): return self._x
            """
        )

        assert "x" not in result, "one-line method should not get its own chunk"
        assert "def x(self)" in result["Point"].content


class TestModuleLevelAndNestedConstructs:
    """Constructs reachable only by walking past the top level, or past methods.

    These were the H1/H3 gaps: without them a constants module is invisible to
    retrieval and a schema class indexes as a bare header line.
    """

    def test_module_level_constants_are_indexed(self):
        result = by_symbol(
            """
            import os

            MAX_RETRIES = 3
            DEFAULT_TIMEOUT = 30
            """
        )

        module = result["sample.py"]
        assert module.chunk_type == "module"
        assert module.class_name is None
        assert "MAX_RETRIES = 3" in module.content
        assert "import os" in module.content

    def test_definitions_guarded_by_type_checking_are_indexed(self):
        result = by_symbol(
            """
            if TYPE_CHECKING:
                def helper(value):
                    return value
            """
        )

        assert "helper" in result

    def test_definitions_inside_a_try_block_are_indexed(self):
        result = by_symbol(
            """
            try:
                def parse(raw):
                    return raw
            except ImportError:
                pass
            """
        )

        assert "parse" in result

    def test_attributes_only_class_indexes_more_than_its_bare_header(self):
        skeleton = by_symbol(
            """
            class Settings(Base):
                \"\"\"Runtime configuration.\"\"\"

                host: str = "localhost"
                port: int = 5432
            """
        )["Settings"]

        assert "host" in skeleton.content
        assert "port" in skeleton.content

    def test_nested_classes_are_reachable(self):
        result = by_symbol(
            """
            class Outer:
                class Meta:
                    ordering = ["id"]

                def run(self):
                    return 1
            """
        )

        assert "Meta" in result or "Meta" in result["Outer"].content

    def test_decorator_does_not_change_whether_a_short_method_folds(self):
        """The fold threshold measures the definition, not the decorated wrapper.

        Measuring the wrapper made `@property def x` span 2 lines and earn a
        standalone chunk, while the identical undecorated method spanned 1 and
        folded away.
        """
        undecorated = by_symbol(
            """
            class Point:
                def x(self): return self._x
            """
        )
        decorated = by_symbol(
            """
            class Point:
                @property
                def x(self): return self._x
            """
        )

        assert ("x" in undecorated) == ("x" in decorated)
