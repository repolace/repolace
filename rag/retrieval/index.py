import asyncio
import os
import subprocess
import uuid
from pathlib import Path

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_shared.db.models import CodeChunk, RegisteredRepo
from repolace_shared.git.repo import UNTRUSTED_TREE_CONFIG_ARGS, sanitized_git_env
from repolace_shared.paths import PathEscapesRoot, resolve_within
from retrieval.chunker import Chunk, chunk_python_file
from retrieval.config import EMBED_BATCH_SIZE
from retrieval.embed import embed_texts

log = structlog.get_logger()

#: Generous for real source; a file past it is generated or hostile, and
#: either way is not what retrieval is for.
MAX_SOURCE_BYTES = 2 * 1024 * 1024

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
    # followlinks=False is the default, but state it: it is the half of the
    # symlink defence that lives here, and a later "tidy-up" that flipped it
    # would reopen the hole silently.
    for dirpath, dirnames, filenames in os.walk(repo_path, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
        total_files += len(filenames)
        for name in filenames:
            if not name.endswith(".py"):
                continue
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                # os.walk already refuses to descend a symlinked *directory*;
                # a symlinked file is the half it does not cover, and that half
                # is enough: a tracked `config.py -> /home/worker/.env` puts a
                # host file into code_chunks.content, from where it is
                # retrievable and lands in an LLM prompt.
                log.warning("rag.index.symlink_skipped", file_path=str(candidate))
                continue
            found.append(candidate)

    if not found:
        log.warning(
            "rag.index.no_python_files",
            repo_path=str(repo_path),
            total_files=total_files,
            detail="indexing supports Python only; this repo will have an empty index",
        )
    return sorted(found)


def chunk_file(repo_path: Path, file_path: Path) -> list[Chunk]:
    """Read one file and chunk it, refusing anything that leaves the checkout.

    The containment check matters here and not only in `find_python_files`,
    because the incremental path never goes through that function:
    `_incremental_index` builds `repo_path / rel` straight from
    `git diff --name-only`, and git tracks a symlink as an ordinary mode-120000
    entry. The incremental path is also the *common* path once a repo has been
    indexed once, so this is the live one.

    `relative_path` stays **lexical**, deliberately. It is the join key
    `_incremental_index` deletes on, and it has to equal the string git emitted;
    resolving it would silently stop matching stored rows for any repo reached
    through a symlinked directory. Containment is checked separately, on the
    resolved path.
    """
    relative_path = str(file_path.relative_to(repo_path))
    try:
        resolve_within(repo_path, relative_path)
    except (PathEscapesRoot, OSError) as exc:
        log.warning("rag.index.outside_repo", file_path=relative_path, error=str(exc))
        return []
    try:
        size = file_path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            # The tree is untrusted, and read_text has no bound of its own: a
            # generated multi-hundred-megabyte .py would go into memory whole
            # and then into tree-sitter.
            log.warning("rag.index.oversized_file", file_path=relative_path, size=size)
            return []
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


#: These two commands finish in milliseconds on any repo; a minute means git is
#: wedged, not slow.
GIT_TIMEOUT_SECONDS = 60.0


def _git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git against the checkout with the same config pins and environment
    allowlist `repolace_shared.git.repo` uses, minus its async wrapper.

    The environment is the sharper half. This used to inherit all of
    `os.environ` -- so git, and anything git spawned, held the GitHub App
    private key and the database URL. The private key is worse than any single
    token: it mints installation tokens for every installation, and rotating
    tokens does not revoke it.

    Deliberately still synchronous and still `check=True`. The caller's
    `CalledProcessError` handling is load-bearing (`reindex_if_stale` falls back
    to a full reindex on it), and converting to the async `run_git` would change
    the exception type across that boundary. Unifying the two wrappers is a
    known duplication and a deliberate deferral, recorded in CLAUDE.md.
    """
    return subprocess.run(
        ["git", *UNTRUSTED_TREE_CONFIG_ARGS, *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
        env=sanitized_git_env(),
        stdin=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_SECONDS,
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
