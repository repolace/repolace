import subprocess
import uuid
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_shared.db.models import RegisteredRepo
from repolace_shared.db.models import CodeChunk
from retrieval.chunker import Chunk, chunk_python_file
from retrieval.embed import embed_texts

_IGNORED_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache", "dist", "build",
}


def find_python_files(repo_path: Path) -> list[Path]:
    return sorted(
        path
        for path in repo_path.rglob("*.py")
        if not any(part in _IGNORED_DIR_NAMES for part in path.relative_to(repo_path).parts)
    )


def chunk_file(repo_path: Path, file_path: Path) -> list[Chunk]:
    relative_path = str(file_path.relative_to(repo_path))
    source = file_path.read_text(encoding="utf-8", errors="ignore")
    return chunk_python_file(relative_path, source)


async def _insert_chunks(db: AsyncSession, repo_id: uuid.UUID, commit_sha: str, chunks: list[Chunk]) -> None:
    if not chunks:
        return
    embeddings = embed_texts([chunk.content for chunk in chunks])
    db.add_all(
        CodeChunk(
            repo_id=repo_id,
            commit_sha=commit_sha,
            file_path=chunk.file_path,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            chunk_type=chunk.chunk_type,
            class_name=chunk.class_name,
            symbol_name=chunk.symbol_name,
            content=chunk.content,
            embedding=embedding,
        )
        for chunk, embedding in zip(chunks, embeddings)
    )


async def index_repo(db: AsyncSession, repo_id: uuid.UUID, repo_path: Path, commit_sha: str) -> int:
    """Full (re)index of a repo checkout at commit_sha. Replaces all existing chunks for repo_id."""
    await db.execute(delete(CodeChunk).where(CodeChunk.repo_id == repo_id))

    chunks: list[Chunk] = []
    for file_path in find_python_files(repo_path):
        chunks.extend(chunk_file(repo_path, file_path))

    await _insert_chunks(db, repo_id, commit_sha, chunks)
    await db.execute(
        update(RegisteredRepo).where(RegisteredRepo.id == repo_id).values(indexed_commit_sha=commit_sha)
    )
    await db.commit()
    return len(chunks)


def _changed_files(repo_path: Path, old_sha: str, new_sha: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", old_sha, new_sha],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


async def reindex_if_stale(db: AsyncSession, repo_id: uuid.UUID, repo_path: Path, current_commit_sha: str) -> int:
    """Incrementally reindex only files that changed since the last indexed commit.

    Returns the number of chunks (re)written; 0 if the index is already current.
    """
    repo = await db.get(RegisteredRepo, repo_id)
    if repo is None:
        raise ValueError(f"unknown repo_id {repo_id}")

    if repo.indexed_commit_sha is None:
        return await index_repo(db, repo_id, repo_path, current_commit_sha)
    if repo.indexed_commit_sha == current_commit_sha:
        return 0

    changed_files = _changed_files(repo_path, repo.indexed_commit_sha, current_commit_sha)
    changed_py_files = [f for f in changed_files if f.endswith(".py")]

    if changed_py_files:
        await db.execute(
            delete(CodeChunk).where(CodeChunk.repo_id == repo_id, CodeChunk.file_path.in_(changed_py_files))
        )

        chunks: list[Chunk] = []
        for relative_path in changed_py_files:
            full_path = repo_path / relative_path
            if full_path.exists():
                chunks.extend(chunk_file(repo_path, full_path))
        await _insert_chunks(db, repo_id, current_commit_sha, chunks)
    else:
        chunks = []

    await db.execute(
        update(RegisteredRepo).where(RegisteredRepo.id == repo_id).values(indexed_commit_sha=current_commit_sha)
    )
    await db.commit()
    return len(chunks)


async def get_current_chunk_count(db: AsyncSession, repo_id: uuid.UUID) -> int:
    result = await db.execute(select(CodeChunk.id).where(CodeChunk.repo_id == repo_id))
    return len(result.all())
