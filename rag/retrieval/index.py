import asyncio
import os
import subprocess
import uuid
from pathlib import Path

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_shared.db.models import CodeChunk, RegisteredRepo
from retrieval.chunker import Chunk, chunk_python_file
from retrieval.config import EMBED_BATCH_SIZE
from retrieval.embed import embed_texts

log = structlog.get_logger()

_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
    }
)


class RepoIndexInProgress(RuntimeError):
    """Raised when another transaction is already indexing this repo."""


def _repo_lock_key(repo_id: uuid.UUID) -> int:
    """A stable signed 64-bit advisory-lock key derived from the repo UUID."""
    return int.from_bytes(repo_id.bytes[:8], "big", signed=True)


async def _acquire_repo_lock(db: AsyncSession, repo_id: uuid.UUID) -> None:
    """Enforce single-flight indexing per repo.

    Two concurrent indexes of one repo would interleave read-sha, delete,
    insert and update-sha, silently leaving an incomplete index. The lock is
    transaction-scoped, so it is released by the commit or rollback below.
    """
    acquired = await db.scalar(select(func.pg_try_advisory_xact_lock(_repo_lock_key(repo_id))))
    if not acquired:
        raise RepoIndexInProgress(f"repo {repo_id} is already being indexed")


def find_python_files(repo_path: Path) -> list[Path]:
    """Walk the checkout, pruning ignored directories instead of filtering after.

    Indexing is Python-only for now (see CLAUDE.md). A repo in another language
    yields no chunks, which is indistinguishable from a successful index unless
    it is called out, so the ratio is logged and an empty result warns.
    """
    found: list[Path] = []
    total_files = 0
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
        total_files += len(filenames)
        found.extend(Path(dirpath) / name for name in filenames if name.endswith(".py"))

    if not found:
        log.warning(
            "rag.index.no_python_files",
            repo_path=str(repo_path),
            total_files=total_files,
            detail="indexing supports Python only; this repo will have an empty index",
        )
    return sorted(found)


def chunk_file(repo_path: Path, file_path: Path) -> list[Chunk]:
    relative_path = str(file_path.relative_to(repo_path))
    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        # One unreadable file (permissions, broken symlink) must not abort the
        # whole repo index.
        log.warning("rag.index.unreadable_file", file_path=relative_path, error=str(exc))
        return []
    return chunk_python_file(relative_path, source)


def _chunk_paths(repo_path: Path, paths: list[Path]) -> list[Chunk]:
    """Read and parse files. Blocking; call via asyncio.to_thread."""
    chunks: list[Chunk] = []
    for path in paths:
        chunks.extend(chunk_file(repo_path, path))
    return chunks


async def _insert_chunks(db: AsyncSession, repo_id: uuid.UUID, commit_sha: str, chunks: list[Chunk]) -> None:
    """Embed and insert in batches.

    Embedding a whole repo in one call materialises every vector as Python
    floats at once — several GB on a large repo — so this bounds both the
    inference working set and the size of any single flush.
    """
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start : start + EMBED_BATCH_SIZE]
        # SentenceTransformer.encode is synchronous and CPU-bound.
        embeddings = await asyncio.to_thread(embed_texts, [chunk.content for chunk in batch])
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
            # strict: a short embedding list must raise, not silently drop chunks.
            for chunk, embedding in zip(batch, embeddings, strict=True)
        )
        await db.flush()


async def _set_indexed_sha(db: AsyncSession, repo_id: uuid.UUID, commit_sha: str) -> None:
    await db.execute(
        update(RegisteredRepo).where(RegisteredRepo.id == repo_id).values(indexed_commit_sha=commit_sha)
    )


async def _full_index(db: AsyncSession, repo_id: uuid.UUID, repo_path: Path, commit_sha: str) -> int:
    paths = await asyncio.to_thread(find_python_files, repo_path)
    chunks = await asyncio.to_thread(_chunk_paths, repo_path, paths)

    # Delete before inserting rather than filtering by commit_sha afterwards:
    # re-indexing the *same* commit must replace its rows, not duplicate them.
    # Holding these row locks for the batch run is acceptable because the
    # advisory lock above already makes indexing single-flight per repo, and
    # Postgres readers are not blocked by writers.
    await db.execute(delete(CodeChunk).where(CodeChunk.repo_id == repo_id))
    await _insert_chunks(db, repo_id, commit_sha, chunks)
    await _set_indexed_sha(db, repo_id, commit_sha)

    log.info(
        "rag.index.full",
        repo_id=str(repo_id),
        commit_sha=commit_sha,
        file_count=len(paths),
        chunk_count=len(chunks),
    )
    return len(chunks)


async def index_repo(db: AsyncSession, repo_id: uuid.UUID, repo_path: Path, commit_sha: str) -> int:
    """Full (re)index of a repo checkout at commit_sha. Replaces all existing chunks.

    Owns its transaction: this is one unit of work, and the advisory lock that
    makes it single-flight lives for the transaction's lifetime.
    """
    await _acquire_repo_lock(db, repo_id)
    try:
        count = await _full_index(db, repo_id, repo_path, commit_sha)
        await db.commit()
        return count
    except Exception:
        await db.rollback()
        raise


def _git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )


def _assert_git_root(repo_path: Path) -> None:
    """git reports paths relative to the repo root, so anything else mismatches."""
    toplevel = Path(_git(repo_path, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if toplevel != repo_path.resolve():
        raise ValueError(f"repo_path {repo_path} is not the git root ({toplevel})")


def _changed_files(repo_path: Path, old_sha: str, new_sha: str) -> list[str]:
    # -z plus core.quotepath=false: without them git C-quotes non-ASCII paths
    # (e.g. "src/caf\303\251.py"), which matches neither the stored file_path
    # nor an on-disk lookup, so such files silently keep stale chunks.
    result = _git(repo_path, "diff", "--name-only", "-z", old_sha, new_sha)
    return [path for path in result.stdout.split("\0") if path]


async def _incremental_index(
    db: AsyncSession,
    repo_id: uuid.UUID,
    repo_path: Path,
    changed_py_files: list[str],
    old_sha: str,
    new_sha: str,
) -> int:
    chunks: list[Chunk] = []
    if changed_py_files:
        await db.execute(
            delete(CodeChunk).where(CodeChunk.repo_id == repo_id, CodeChunk.file_path.in_(changed_py_files))
        )
        present = [repo_path / rel for rel in changed_py_files if (repo_path / rel).exists()]
        chunks = await asyncio.to_thread(_chunk_paths, repo_path, present)
        await _insert_chunks(db, repo_id, new_sha, chunks)

    await _set_indexed_sha(db, repo_id, new_sha)
    log.info(
        "rag.index.incremental",
        repo_id=str(repo_id),
        old_sha=old_sha,
        new_sha=new_sha,
        changed_file_count=len(changed_py_files),
        chunk_count=len(chunks),
    )
    return len(chunks)


async def reindex_if_stale(db: AsyncSession, repo_id: uuid.UUID, repo_path: Path, current_commit_sha: str) -> int:
    """Incrementally reindex only files changed since the last indexed commit.

    Returns the number of chunks (re)written; 0 if the index is already current.
    Owns its transaction, as index_repo does.
    """
    await _acquire_repo_lock(db, repo_id)
    try:
        repo = await db.get(RegisteredRepo, repo_id)
        if repo is None:
            raise ValueError(f"unknown repo_id {repo_id}")

        await asyncio.to_thread(_assert_git_root, repo_path)

        if repo.indexed_commit_sha is None:
            count = await _full_index(db, repo_id, repo_path, current_commit_sha)
        elif repo.indexed_commit_sha == current_commit_sha:
            count = 0
        else:
            try:
                changed = await asyncio.to_thread(
                    _changed_files, repo_path, repo.indexed_commit_sha, current_commit_sha
                )
            except subprocess.CalledProcessError as exc:
                # The recorded commit is unreachable: force-push, shallow clone,
                # or gc. Without this fallback the repo could never reindex,
                # because indexed_commit_sha never returns to None.
                log.warning(
                    "rag.index.diff_failed",
                    repo_id=str(repo_id),
                    old_sha=repo.indexed_commit_sha,
                    new_sha=current_commit_sha,
                    stderr=exc.stderr.strip() if exc.stderr else "",
                )
                count = await _full_index(db, repo_id, repo_path, current_commit_sha)
            else:
                count = await _incremental_index(
                    db,
                    repo_id,
                    repo_path,
                    [f for f in changed if f.endswith(".py")],
                    repo.indexed_commit_sha,
                    current_commit_sha,
                )

        await db.commit()
        return count
    except Exception:
        await db.rollback()
        raise


async def get_current_chunk_count(db: AsyncSession, repo_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count()).select_from(CodeChunk).where(CodeChunk.repo_id == repo_id)
    )
