from dataclasses import dataclass

import structlog
import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from retrieval.config import MIN_STANDALONE_CHUNK_LINES

log = structlog.get_logger()

PY_LANGUAGE = Language(tspython.language())
_PARSER = Parser(PY_LANGUAGE)

# Compound statements whose bodies hold definitions we still want to reach:
# `if TYPE_CHECKING:`, `try:/except ImportError:`, `with`, and friends. Walking
# through these is what keeps conditionally-defined symbols in the index.
_CONTAINER_TYPES = frozenset(
    {
        "block",
        "if_statement",
        "else_clause",
        "elif_clause",
        "try_statement",
        "except_clause",
        "finally_clause",
        "with_statement",
        "for_statement",
        "while_statement",
        "match_statement",
        "case_clause",
    }
)


@dataclass(frozen=True)
class Chunk:
    file_path: str
    start_line: int
    end_line: int
    chunk_type: str
    class_name: str | None
    symbol_name: str
    content: str


def _unwrap_decorated(node: Node) -> Node:
    if node.type != "decorated_definition":
        return node
    for child in node.children:
        if child.type in ("function_definition", "class_definition"):
            return child
    return node


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _def_name(node: Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return "<anonymous>"
    return name_node.text.decode("utf-8")


def _line_span(node: Node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _header_text(source: bytes, node: Node) -> str:
    """The `def foo(...) -> X:` / `class Foo(Base):` line(s), without the body."""
    body = node.child_by_field_name("body")
    end_byte = body.start_byte if body is not None else node.end_byte
    return source[node.start_byte : end_byte].decode("utf-8", errors="replace").rstrip()


def _decorator_text(source: bytes, outer_node: Node, actual_node: Node) -> str:
    """Decorator lines attached to a definition, or '' when there are none."""
    if outer_node is actual_node or outer_node.type != "decorated_definition":
        return ""
    return source[outer_node.start_byte : actual_node.start_byte].decode("utf-8", errors="replace").rstrip()


def _docstring(source: bytes, node: Node) -> str | None:
    body = node.child_by_field_name("body")
    if body is None or not body.children:
        return None
    first_stmt = body.children[0]
    if (
        first_stmt.type == "expression_statement"
        and first_stmt.children
        and first_stmt.children[0].type == "string"
    ):
        return _node_text(source, first_stmt)
    return None


def _signature_and_docstring(source: bytes, outer_node: Node, func_node: Node) -> str:
    """A method's decorators + signature + docstring, for the class skeleton."""
    parts = []
    decorators = _decorator_text(source, outer_node, func_node)
    if decorators:
        parts.append(decorators)

    header = _header_text(source, func_node)
    docstring = _docstring(source, func_node)
    parts.append(f"{header}\n    {docstring}" if docstring else f"{header} ...")
    return "\n".join(parts)


def _iter_definitions(node: Node):
    """Yield (outer, actual) definition pairs reachable without entering a def or class body.

    Descends through compound statements so that a def nested inside
    `if TYPE_CHECKING:` or `try:` is still found, but stops at each definition
    rather than recursing into it — inner functions stay part of their parent's
    content instead of being pulled out as separate chunks.
    """
    for child in node.children:
        actual = _unwrap_decorated(child)
        if actual.type in ("function_definition", "class_definition"):
            yield child, actual
        elif actual.type in _CONTAINER_TYPES:
            yield from _iter_definitions(actual)


def _chunk_class(file_path: str, source: bytes, outer_node: Node, class_node: Node) -> list[Chunk]:
    class_name = _def_name(class_node)
    body = class_node.child_by_field_name("body")

    skeleton_parts = []
    decorators = _decorator_text(source, outer_node, class_node)
    if decorators:
        skeleton_parts.append(decorators)
    skeleton_parts.append(_header_text(source, class_node))

    child_chunks: list[Chunk] = []
    if body is not None:
        for stmt in body.children:
            actual_stmt = _unwrap_decorated(stmt)

            if actual_stmt.type == "function_definition":
                skeleton_parts.append(_signature_and_docstring(source, stmt, actual_stmt))

                # Measure the definition itself, not the decorated wrapper: a
                # one-line method should fold whether or not it has a decorator.
                method_start, method_end = _line_span(actual_stmt)
                if method_end - method_start + 1 < MIN_STANDALONE_CHUNK_LINES:
                    continue

                child_chunks.append(
                    Chunk(
                        file_path=file_path,
                        start_line=_line_span(stmt)[0],
                        end_line=method_end,
                        chunk_type="method",
                        class_name=class_name,
                        symbol_name=_def_name(actual_stmt),
                        content=_node_text(source, stmt),
                    )
                )

            elif actual_stmt.type == "class_definition":
                # Nested class: keep its header in the parent skeleton so the
                # relationship is visible, and index it in its own right.
                skeleton_parts.append(_header_text(source, actual_stmt))
                child_chunks.extend(_chunk_class(file_path, source, stmt, actual_stmt))

            else:
                # Docstrings, class attributes, dataclass fields, ORM columns,
                # Enum members, TypedDict keys — the substance of a schema class.
                skeleton_parts.append(_node_text(source, stmt))

    outer_start, outer_end = _line_span(outer_node)
    skeleton_chunk = Chunk(
        file_path=file_path,
        start_line=outer_start,
        end_line=outer_end,
        chunk_type="class_skeleton",
        class_name=class_name,
        symbol_name=class_name,
        content="\n\n".join(skeleton_parts),
    )
    return [skeleton_chunk, *child_chunks]


def _module_chunk(file_path: str, source: bytes, statements: list[Node]) -> Chunk | None:
    """Top-level code that is not a definition: imports, constants, module docstring."""
    if not statements:
        return None
    return Chunk(
        file_path=file_path,
        start_line=_line_span(statements[0])[0],
        end_line=_line_span(statements[-1])[1],
        chunk_type="module",
        class_name=None,
        symbol_name=file_path,
        content="\n".join(_node_text(source, stmt) for stmt in statements),
    )


def chunk_python_file(file_path: str, source_text: str) -> list[Chunk]:
    source = source_text.encode("utf-8")
    tree = _PARSER.parse(source)

    if tree.root_node.has_error:
        # tree-sitter recovers rather than raising, so without this a file that
        # fails to parse just yields fewer chunks and looks like a success.
        log.warning("rag.chunker.parse_error", file_path=file_path)

    chunks: list[Chunk] = []
    module_statements: list[Node] = []

    for top_node in tree.root_node.children:
        actual_node = _unwrap_decorated(top_node)

        if actual_node.type == "function_definition":
            start, end = _line_span(top_node)
            chunks.append(
                Chunk(
                    file_path=file_path,
                    start_line=start,
                    end_line=end,
                    chunk_type="function",
                    class_name=None,
                    symbol_name=_def_name(actual_node),
                    content=_node_text(source, top_node),
                )
            )
        elif actual_node.type == "class_definition":
            chunks.extend(_chunk_class(file_path, source, top_node, actual_node))
        elif actual_node.type in _CONTAINER_TYPES:
            nested = list(_iter_definitions(actual_node))
            if nested:
                for outer, inner in nested:
                    if inner.type == "class_definition":
                        chunks.extend(_chunk_class(file_path, source, outer, inner))
                    else:
                        start, end = _line_span(outer)
                        chunks.append(
                            Chunk(
                                file_path=file_path,
                                start_line=start,
                                end_line=end,
                                chunk_type="function",
                                class_name=None,
                                symbol_name=_def_name(inner),
                                content=_node_text(source, outer),
                            )
                        )
            else:
                module_statements.append(top_node)
        else:
            module_statements.append(top_node)

    module_chunk = _module_chunk(file_path, source, module_statements)
    if module_chunk is not None:
        chunks.append(module_chunk)

    return chunks
