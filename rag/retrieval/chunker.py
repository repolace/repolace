from dataclasses import dataclass

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from retrieval.config import MIN_STANDALONE_CHUNK_LINES

PY_LANGUAGE = Language(tspython.language())


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
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _def_name(node: Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return "<anonymous>"
    return name_node.text.decode("utf-8")


def _line_span(node: Node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _signature_and_docstring(source: bytes, func_node: Node) -> str:
    body = func_node.child_by_field_name("body")
    header_end_byte = body.start_byte if body is not None else func_node.end_byte
    header = source[func_node.start_byte : header_end_byte].decode("utf-8").rstrip()

    docstring = None
    if body is not None and body.children:
        first_stmt = body.children[0]
        if (
            first_stmt.type == "expression_statement"
            and first_stmt.children
            and first_stmt.children[0].type == "string"
        ):
            docstring = _node_text(source, first_stmt)

    if docstring:
        return f"{header}\n    {docstring}"
    return f"{header} ..."


def _chunk_class(file_path: str, source: bytes, outer_node: Node, class_node: Node) -> list[Chunk]:
    class_name = _def_name(class_node)
    body = class_node.child_by_field_name("body")

    header_end_byte = body.start_byte if body is not None else class_node.end_byte
    skeleton_parts = [source[class_node.start_byte : header_end_byte].decode("utf-8").rstrip()]

    method_chunks: list[Chunk] = []
    if body is not None:
        for stmt in body.children:
            actual_stmt = _unwrap_decorated(stmt)
            if actual_stmt.type != "function_definition":
                continue

            method_start, method_end = _line_span(stmt)
            skeleton_parts.append(_signature_and_docstring(source, actual_stmt))

            if method_end - method_start + 1 < MIN_STANDALONE_CHUNK_LINES:
                continue

            method_chunks.append(
                Chunk(
                    file_path=file_path,
                    start_line=method_start,
                    end_line=method_end,
                    chunk_type="method",
                    class_name=class_name,
                    symbol_name=_def_name(actual_stmt),
                    content=_node_text(source, stmt),
                )
            )

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
    return [skeleton_chunk, *method_chunks]


def chunk_python_file(file_path: str, source_text: str) -> list[Chunk]:
    source = source_text.encode("utf-8")
    tree = Parser(PY_LANGUAGE).parse(source)

    chunks: list[Chunk] = []
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

    return chunks
