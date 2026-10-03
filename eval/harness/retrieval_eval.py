"""Does retrieval find the code the fix changed? Measured, per embedding strategy.

For each upstream repository and each embedding strategy this indexes the repo at
every instance's base commit (incrementally, instances in base-commit date order,
so the second instance costs a diff and not a re-embedding), runs the query
production would run for that issue, and scores where the gold patch's files and
old-side hunks land in the ranking. No LLM is called.

**Cost is the constraint, so the run is planned before anything is embedded.**
Indexing is `repos x strategies` full indexes plus the incremental passes between
instances. `--plan` counts chunks at each repository's earliest base commit
(parsing only, no embedding) and prints the bill; a real run refuses if
`repos x strategies` exceeds `--max-index-equivalents` (default 35, the budget
for a CPU run), and skips -- loudly, never silently -- any repository whose
first index would exceed `--max-chunks-per-repo`. A repository is skipped whole
rather than trimmed to a subset of its files: a smaller corpus than production
searches would make every recall number look better than it is.

**Eval rows must be invisible to the product.** Each (repo, strategy) gets one
`registered_repos` row under a synthetic installation (id -1), with a negative
`github_repo_id` derived from a stable hash, and `is_active=False`. The API's
installation sync only ever touches rows of the installation id GitHub names, so
these are never listed, deactivated or reactivated; a test pins that. The rows
(and their chunks) persist between runs on purpose, so a re-run is incremental;
`DELETE FROM registered_repos WHERE installation_id = -1` removes them.

**What the numbers mean** (see `harness.metrics` for the arithmetic): file recall
is over distinct files; chunk recall is over gold *hunks* (a hunk is recalled if
any top-k chunk overlaps it); a query with no old-side gold (a patch that only
adds files) is skipped and counted, not scored as a miss; an insertion is
anchored to the old line it follows. The gold patch is read from the
`<id>.gold.patch` that `select` writes beside each instance. It never reaches an
agent: this module reads it, and the pipeline never does.

Importing this module is cheap. `retrieval.*` (which pulls the embedding model
library) is imported inside `load_retrieval_api`, once, when a run starts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import sys
import tempfile
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness.metrics import (
    DEFAULT_KS,
    AggregateMetrics,
    QueryMetrics,
    Span,
    aggregate_metrics,
    score_query,
)
from harness.patches import DELETED, MODIFIED, RENAMED, PatchError, files_in_patch, old_side_hunks
from harness.select_instances import GOLD_PATCH_SUFFIX, SelectError, cache_path
from repolace_shared.db.models import CodeChunk, GithubInstallation, RegisteredRepo
from repolace_shared.git.repo import run_git
from repolace_shared.instances import InstanceError, InstanceSpec, load_instances
from repolace_shared.paths import PathEscapesRoot, resolve_within
from verify.scoring import is_protected_path

STRATEGIES = ("truncate", "head_tail", "windows", "signature_docstring")
#: The query side: how an over-length query is shortened before it is embedded.
QUERY_STRATEGIES = ("truncate", "head_tail")
SEARCH_LIMIT = max(DEFAULT_KS)

EVAL_INSTALLATION_ID = -1
EVAL_ACCOUNT_LOGIN = "repolace-eval"
EVAL_OWNER = "eval"

DEFAULT_MAX_INDEX_EQUIVALENTS = 35
#: A guardrail on one repository's first index, not a measured threshold: tune it
#: with `--plan` output in hand.
DEFAULT_MAX_CHUNKS_PER_REPO = 40_000


class RetrievalEvalError(RuntimeError):
    """The eval cannot run as asked (budget, missing cache, tampered eval rows)."""


class RetrievalUnavailable(RetrievalEvalError):
    """The `retrieval` package lacks the strategy-aware API this eval is written against."""


@dataclass(frozen=True)
class Query:
    semantic: str
    keyword: str


@dataclass(frozen=True)
class RetrievalApi:
    """The four things the eval needs from `retrieval`, so tests can substitute them."""

    #: (session, repo_id, checkout path, commit sha, strategy) -> chunks written
    reindex: Callable[[AsyncSession, uuid.UUID, Path, str, str], Awaitable[int]]
    #: (session, repo_id, query, limit, query_strategy) -> chunk spans in rank order
    search: Callable[[AsyncSession, uuid.UUID, Query, int, str], Awaitable[list[Span]]]
    build_query: Callable[[str, str | None], Query]
    #: chunk count of a checkout, without embedding anything
    count_chunks: Callable[[Path], int]
    #: (session, repo_id, paths) -> the indexed chunks of those files, so the chunk
    #: metrics can tell a chunk's span from its content (see `harness.metrics`)
    chunk_spans: Callable[[AsyncSession, uuid.UUID, Sequence[str]], Awaitable[list[Span]]]


async def indexed_chunk_spans(session: AsyncSession, repo_id: uuid.UUID, paths: Sequence[str]) -> list[Span]:
    """The spans of every indexed chunk of `paths` in one repo, as the index holds them."""
    if not paths:
        return []
    rows = await session.execute(
        select(CodeChunk.file_path, CodeChunk.start_line, CodeChunk.end_line)
        .where(CodeChunk.repo_id == repo_id, CodeChunk.file_path.in_(list(paths)))
    )
    return [Span(path, start, end) for path, start, end in rows.all()]


def _require_parameters(function: Callable, names: set[str], label: str) -> None:
    missing = names - set(inspect.signature(function).parameters)
    if missing:
        raise RetrievalUnavailable(
            f"{label} has no parameter(s) {', '.join(sorted(missing))}: the strategy-aware retrieval "
            f"API (stream C) is not present in this checkout"
        )


def load_retrieval_api() -> RetrievalApi:
    """Bind to `retrieval`, or say exactly what is missing. Imports the model library."""
    try:
        from retrieval.index import chunk_file, find_python_files, reindex_if_stale
        from retrieval.query import build_query
        from retrieval.retrieve import hybrid_search
    except ImportError as exc:
        raise RetrievalUnavailable(f"cannot import the retrieval API: {exc}") from exc
    _require_parameters(reindex_if_stale, {"strategy"}, "retrieval.index.reindex_if_stale")
    _require_parameters(hybrid_search, {"keyword_query", "query_strategy"}, "retrieval.retrieve.hybrid_search")

    async def reindex(session: AsyncSession, repo_id: uuid.UUID, path: Path, sha: str, strategy: str) -> int:
        return await reindex_if_stale(session, repo_id, path, sha, strategy=strategy)

    async def search(session: AsyncSession, repo_id: uuid.UUID, query: Query, limit: int, query_strategy: str) -> list[Span]:
        results = await hybrid_search(
            session, repo_id, query.semantic, limit=limit,
            keyword_query=query.keyword, query_strategy=query_strategy,
        )
        return [Span(r.chunk.file_path, r.chunk.start_line, r.chunk.end_line) for r in results]

    def make_query(title: str, body: str | None) -> Query:
        built = build_query(title, body)
        return Query(semantic=built.semantic, keyword=built.keyword)

    def count_chunks(path: Path) -> int:
        return sum(len(chunk_file(path, file)) for file in find_python_files(path))

    return RetrievalApi(
        reindex=reindex, search=search, build_query=make_query, count_chunks=count_chunks, chunk_spans=indexed_chunk_spans,
    )


# --- eval-only rows ----------------------------------------------------------


def eval_github_repo_id(repo: str, strategy: str) -> int:
    """A stable negative 63-bit id for one (repo, strategy). No real repository has one."""
    digest = hashlib.sha256(f"{repo}@{strategy}".encode()).digest()
    return -((int.from_bytes(digest[:8], "big") & (2**63 - 1)) or 1)


async def ensure_eval_repo(session: AsyncSession, repo: str, strategy: str) -> RegisteredRepo:
    """Get or create the eval row for one (repo, strategy). Idempotent.

    A row found under the eval id that is active, or belongs to another
    installation, was edited by something else; it is refused rather than reused,
    because indexing into a row the product can see is exactly what the synthetic
    ids exist to prevent.
    """
    if strategy not in STRATEGIES:
        raise RetrievalEvalError(f"unknown embedding strategy {strategy!r}")
    if await session.get(GithubInstallation, EVAL_INSTALLATION_ID) is None:
        session.add(GithubInstallation(
            id=EVAL_INSTALLATION_ID, account_login=EVAL_ACCOUNT_LOGIN, account_id=-1, account_type="Organization",
        ))
        await session.flush()
    github_repo_id = eval_github_repo_id(repo, strategy)
    row = (
        await session.execute(select(RegisteredRepo).where(RegisteredRepo.github_repo_id == github_repo_id))
    ).scalar_one_or_none()
    if row is None:
        row = RegisteredRepo(
            installation_id=EVAL_INSTALLATION_ID, github_repo_id=github_repo_id, owner=EVAL_OWNER,
            name=f"{repo}@{strategy}", full_name=f"{EVAL_OWNER}/{repo}@{strategy}",
            default_branch="eval", private=True, is_active=False,
        )
        session.add(row)
    elif row.installation_id != EVAL_INSTALLATION_ID or row.is_active:
        raise RetrievalEvalError(
            f"{row.full_name} (github_repo_id {github_repo_id}) is not an inactive eval row of installation "
            f"{EVAL_INSTALLATION_ID}; refusing to index into it"
        )
    await session.commit()
    return row


# --- the repository and the instances ----------------------------------------


class RepoSource(Protocol):
    """One upstream repository, checked out at whichever commit the eval is on."""

    path: Path

    async def commit_time(self, sha: str) -> int: ...

    async def checkout(self, sha: str) -> None: ...


@dataclass
class GitSource:
    path: Path

    async def commit_time(self, sha: str) -> int:
        return int((await run_git("show", "-s", "--format=%ct", sha, cwd=self.path)).strip())

    async def checkout(self, sha: str) -> None:
        # `--force`: nothing here edits the tree, so anything unexpected in it is
        # discarded rather than carried into the next base commit's index.
        await run_git("checkout", "--quiet", "--force", sha, cwd=self.path)


@asynccontextmanager
async def open_git_source(cache_dir: Path, repo: str) -> AsyncIterator[GitSource]:
    """A throwaway working clone of the repo's cache clone, with objects shared.

    The cache is `select`'s: full history, never checked out here, so nothing the
    eval does can disturb the clone the instance selection depends on. A missing
    cache is an error with the instruction, not a network clone: this eval does
    not touch the network.
    """
    cache = cache_path(cache_dir, repo)
    if not (cache / ".git").is_dir():
        raise RetrievalEvalError(f"no cache clone of {repo} at {cache}; run `repolace-eval select` first")
    with tempfile.TemporaryDirectory(prefix="repolace-retrieval-eval-") as scratch:
        work = Path(scratch) / "work"
        await run_git("clone", "--shared", "--quiet", "--no-checkout", str(cache), str(work))
        yield GitSource(work)


@dataclass(frozen=True)
class GoldTargets:
    paths: tuple[str, ...]
    hunks: tuple[Span, ...]
    #: `(path, reason)` for gold files retrieval cannot reach, kept out of the targets and reported.
    dropped: tuple[tuple[str, str], ...] = ()


NOT_PYTHON = "not a Python file: the indexer only chunks .py files"
NO_CHANGED_LINES = "no changed old lines (a mode-only change or a pure rename)"


def gold_targets(patch: str) -> GoldTargets:
    """What the fix changed that retrieval could have found: Python files with changed old lines.

    Files the patch only *adds* are not targets: they are absent from the index at the base
    commit. Neither is a gold file the indexer cannot hold -- the chunker is Python-only, so
    `docs/guide.rst` is in no chunk -- nor one with no changed old line (a mode-only change,
    a pure rename), which has a path and no hunk. Counting any of them would cap recall below
    1.0 for a reason that is not retrieval; they are returned in `dropped` so the report can
    say how many there were. Test and config paths are dropped silently: a gold patch has
    none by construction, and the filter keeps a hand-made instance honest.
    """
    old_paths = [
        (change.old_path if change.status == RENAMED and change.old_path else change.path)
        for change in files_in_patch(patch)
        if change.status in (MODIFIED, DELETED, RENAMED)
    ]
    hunks = old_side_hunks(patch)
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    for path in dict.fromkeys(old_paths):
        if is_protected_path(path):
            continue
        if not path.endswith(".py"):
            dropped.append((path, NOT_PYTHON))
        elif path not in hunks:
            dropped.append((path, NO_CHANGED_LINES))
        else:
            kept.append(path)
    return GoldTargets(
        paths=tuple(kept),
        hunks=tuple(Span(path, start, end) for path in kept for start, end in hunks[path]),
        dropped=tuple(dropped),
    )


@dataclass(frozen=True)
class EvalInstance:
    spec: InstanceSpec
    gold: GoldTargets
    commit_time: int


@dataclass(frozen=True)
class SkippedInstance:
    instance_id: str
    reason: str


async def load_eval_instances(
    specs: Sequence[InstanceSpec],
    instances_dir: Path,
    source: RepoSource,
    *,
    limit: int | None = None,
) -> tuple[list[EvalInstance], list[SkippedInstance]]:
    """The repo's instances in base-commit date order, with their gold targets.

    An instance whose gold patch is missing or unreadable is skipped and listed,
    never evaluated against an empty gold (which would score as nothing found).
    """
    instances: list[EvalInstance] = []
    skipped: list[SkippedInstance] = []
    for spec in specs:
        try:
            sidecar = resolve_within(instances_dir, f"{spec.instance_id}{GOLD_PATCH_SUFFIX}")
        except PathEscapesRoot as exc:
            skipped.append(SkippedInstance(spec.instance_id, f"gold patch path refused: {exc}"))
            continue
        if not sidecar.is_file():
            skipped.append(SkippedInstance(spec.instance_id, f"no {spec.instance_id}{GOLD_PATCH_SUFFIX} beside the instance"))
            continue
        try:
            gold = gold_targets(sidecar.read_text(encoding="utf-8"))
        except (PatchError, UnicodeDecodeError) as exc:
            skipped.append(SkippedInstance(spec.instance_id, f"gold patch unreadable: {exc}"))
            continue
        instances.append(EvalInstance(spec, gold, await source.commit_time(spec.base_commit)))
    instances.sort(key=lambda i: (i.commit_time, i.spec.instance_id))
    return (instances[:limit] if limit else instances), skipped


# --- the run -----------------------------------------------------------------


@dataclass(frozen=True)
class InstanceResult:
    repo: str
    strategy: str
    query_strategy: str
    instance_id: str
    metrics: QueryMetrics
    #: Chunks (re)written to reach this commit: the full index for the first
    #: instance of a (repo, strategy), the changed files' for the rest.
    chunks_written: int


@dataclass(frozen=True)
class RepoPlan:
    repo: str
    instances: int
    #: Chunks at the earliest base commit, i.e. one full index. None if not counted.
    first_index_chunks: int | None
    skip_reason: str | None = None


@dataclass
class EvalRun:
    strategies: tuple[str, ...]
    query_strategies: tuple[str, ...]
    plans: list[RepoPlan]
    results: list[InstanceResult] = field(default_factory=list)
    skipped_instances: list[SkippedInstance] = field(default_factory=list)
    #: `(instance_id, path, reason)`: gold files left out of the targets as unreachable.
    dropped_targets: list[tuple[str, str, str]] = field(default_factory=list)


async def plan_repo(
    api: RetrievalApi, source: RepoSource, repo: str, instances: Sequence[EvalInstance], max_chunks: int,
) -> RepoPlan:
    if not instances:
        return RepoPlan(repo, 0, None, "no instance has a usable gold patch")
    await source.checkout(instances[0].spec.base_commit)
    chunks = await asyncio.to_thread(api.count_chunks, source.path)
    if chunks == 0:
        return RepoPlan(repo, len(instances), 0, "no Python chunks at the earliest base commit")
    if chunks > max_chunks:
        return RepoPlan(
            repo, len(instances), chunks,
            f"first index would embed {chunks} chunks, over --max-chunks-per-repo {max_chunks}; skipped whole "
            f"(trimming it would make recall look better than production's)",
        )
    return RepoPlan(repo, len(instances), chunks)


async def evaluate_repo_strategy(
    session_factory: async_sessionmaker[AsyncSession],
    api: RetrievalApi,
    source: RepoSource,
    repo: str,
    strategy: str,
    instances: Sequence[EvalInstance],
    query_strategies: Sequence[str],
    *,
    progress: Callable[[str], None] = lambda _: None,
) -> list[InstanceResult]:
    """Index `repo` with `strategy` across its instances in date order and score each."""
    results: list[InstanceResult] = []
    async with session_factory() as session:
        row = await ensure_eval_repo(session, repo, strategy)
        repo_id = row.id
        for instance in instances:
            spec = instance.spec
            progress(f"{repo} [{strategy}] {spec.instance_id}")
            await source.checkout(spec.base_commit)
            written = await api.reindex(session, repo_id, source.path, spec.base_commit, strategy)
            # The query production builds for this issue: title and body, not the body alone.
            query = api.build_query(spec.issue_title, spec.problem_statement)
            if not query.semantic.strip():
                raise RetrievalEvalError(f"{spec.instance_id}: the issue yields an empty query")
            # The gold files' chunks as indexed at this commit: what lets the chunk metrics
            # credit the innermost chunk of a hunk and not any wide span that overlaps it.
            corpus = await api.chunk_spans(session, repo_id, instance.gold.paths)
            for query_strategy in query_strategies:
                ranked = await api.search(session, repo_id, query, SEARCH_LIMIT, query_strategy)
                results.append(InstanceResult(
                    repo=repo, strategy=strategy, query_strategy=query_strategy, instance_id=spec.instance_id,
                    metrics=score_query(ranked, instance.gold.paths, instance.gold.hunks, corpus=corpus),
                    chunks_written=written,
                ))
    return results


async def run_eval(
    session_factory: async_sessionmaker[AsyncSession] | None,
    api: RetrievalApi,
    *,
    instances_dir: Path,
    cache_dir: Path,
    repos: Sequence[str] | None = None,
    strategies: Sequence[str] = STRATEGIES,
    query_strategies: Sequence[str] = QUERY_STRATEGIES,
    max_instances_per_repo: int | None = None,
    max_chunks_per_repo: int = DEFAULT_MAX_CHUNKS_PER_REPO,
    max_index_equivalents: int = DEFAULT_MAX_INDEX_EQUIVALENTS,
    plan_only: bool = False,
    open_source: Callable[[Path, str], AsyncIterator[RepoSource]] | None = None,
    progress: Callable[[str], None] = lambda _: None,
) -> EvalRun:
    """Plan, check the budget, then index and score. `plan_only` stops after the plan.

    `repos=None` means every repository that has an instance.
    """
    for strategy in (*strategies, *query_strategies):
        if strategy not in STRATEGIES:
            raise RetrievalEvalError(f"unknown embedding strategy {strategy!r}; known: {', '.join(STRATEGIES)}")
    opener = asynccontextmanager(open_source) if open_source is not None else open_git_source
    run = EvalRun(tuple(strategies), tuple(query_strategies), plans=[])
    loaded: dict[str, list[EvalInstance]] = {}

    all_specs = list(load_instances(instances_dir).values())
    for repo in repos if repos is not None else sorted({spec.repo for spec in all_specs}):
        async with opener(cache_dir, repo) as source:
            instances, skipped = await load_eval_instances(
                [spec for spec in all_specs if spec.repo == repo], instances_dir, source, limit=max_instances_per_repo,
            )
            run.skipped_instances.extend(skipped)
            run.dropped_targets.extend(
                (i.spec.instance_id, path, reason) for i in instances for path, reason in i.gold.dropped
            )
            plan = await plan_repo(api, source, repo, instances, max_chunks_per_repo)
        run.plans.append(plan)
        if plan.skip_reason is None:
            loaded[repo] = instances
        progress(f"{repo}: {plan.instances} instance(s), first index {plan.first_index_chunks} chunk(s)"
                 + (f" -- SKIPPED: {plan.skip_reason}" if plan.skip_reason else ""))

    equivalents = len(loaded) * len(strategies)
    if equivalents > max_index_equivalents:
        raise RetrievalEvalError(
            f"{len(loaded)} repo(s) x {len(strategies)} strategies = {equivalents} full indexes, over "
            f"--max-index-equivalents {max_index_equivalents}; narrow with --repos / --strategies"
        )
    if plan_only or session_factory is None:
        return run

    for repo, instances in loaded.items():
        async with opener(cache_dir, repo) as source:
            for strategy in strategies:
                run.results.extend(await evaluate_repo_strategy(
                    session_factory, api, source, repo, strategy, instances, query_strategies, progress=progress,
                ))
    return run


# --- reporting ---------------------------------------------------------------


def summarize(run: EvalRun) -> dict[tuple[str, str], AggregateMetrics]:
    """Aggregate metrics per (index strategy, query strategy), pooled over repos."""
    grouped: dict[tuple[str, str], list[QueryMetrics]] = defaultdict(list)
    for result in run.results:
        grouped[(result.strategy, result.query_strategy)].append(result.metrics)
    return {key: aggregate_metrics(values) for key, values in sorted(grouped.items())}


def summarize_by_repo(run: EvalRun) -> dict[tuple[str, str, str], AggregateMetrics]:
    grouped: dict[tuple[str, str, str], list[QueryMetrics]] = defaultdict(list)
    for result in run.results:
        grouped[(result.repo, result.strategy, result.query_strategy)].append(result.metrics)
    return {key: aggregate_metrics(values) for key, values in sorted(grouped.items())}


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _row(label: str, agg: AggregateMetrics) -> str:
    cells = [_pct(agg.file_recall[k].mean) for k in DEFAULT_KS] + [_pct(agg.chunk_recall[k].mean) for k in DEFAULT_KS]
    cells += [_pct(agg.file_mrr.mean), _pct(agg.chunk_mrr.mean)]
    scored = f"{agg.file_mrr.scored}/{agg.queries}"
    return f"| {label} | {scored} | " + " | ".join(cells) + " |"


_HEADER = (
    "| {first} | queries with gold | file R@5 | file R@10 | file R@20 | chunk R@5 | chunk R@10 | chunk R@20 "
    "| file MRR | chunk MRR |\n|---|---|---|---|---|---|---|---|---|---|"
)


def to_markdown(run: EvalRun) -> str:
    out = ["# Retrieval eval", ""]
    out += [
        "Gold is the fix's old-side files and changed lines. A query with no old-side gold is not scored "
        "(the `queries with gold` column says how many were). Chunk recall is over gold hunks. "
        "MRR is over the top 20 only; a miss scores 0.",
        "",
        "## Plan",
        "",
        "| repo | instances | first index (chunks) | status |",
        "|---|---|---|---|",
    ]
    for plan in run.plans:
        chunks = "n/a" if plan.first_index_chunks is None else str(plan.first_index_chunks)
        out.append(f"| {plan.repo} | {plan.instances} | {chunks} | {plan.skip_reason or 'evaluated'} |")
    if run.skipped_instances:
        out += ["", "Instances skipped (never scored as misses):", ""]
        out += [f"- {s.instance_id}: {s.reason}" for s in run.skipped_instances]
    if run.dropped_targets:
        out += ["", f"Gold files left out of the targets as unreachable ({len(run.dropped_targets)}):", ""]
        out += [f"- {instance_id}: {path} ({reason})" for instance_id, path, reason in run.dropped_targets]
    summary = summarize(run)
    if summary:
        out += ["", "## By embedding strategy (all repositories pooled)", "", _HEADER.format(first="index / query strategy")]
        out += [_row(f"{index} / {query}", agg) for (index, query), agg in summary.items()]
        out += ["", "## By repository", "", _HEADER.format(first="repo, index / query strategy")]
        out += [_row(f"{repo}, {index} / {query}", agg) for (repo, index, query), agg in summarize_by_repo(run).items()]
        first = [r for r in run.results if r.query_strategy == run.query_strategies[0] and r.strategy == run.strategies[0]]
        unreachable = sum(r.metrics.unreachable_hunks for r in first)
        out += [
            "",
            f"Gold hunks no indexed chunk overlaps: {unreachable}. They are dropped from the chunk metrics and "
            f"not scored as misses; the chunk metrics credit only a hunk's innermost chunks.",
        ]
        written = [r for r in run.results if r.query_strategy == run.query_strategies[0]]
        out += ["", f"Chunks written across the run (first instance of each repo/strategy is the full index): "
                    f"{sum(r.chunks_written for r in written)}."]
    out.append("")
    return "\n".join(out)


def to_json(run: EvalRun) -> str:
    return json.dumps(asdict(run), indent=2, ensure_ascii=False) + "\n"


# --- command line ------------------------------------------------------------


def _csv(value: str) -> list[str]:
    return [part for part in (p.strip() for p in value.split(",")) if part]


def _parser() -> argparse.ArgumentParser:
    eval_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(prog="repolace-eval retrieval-eval", description="Measure retrieval against the gold patches.")
    parser.add_argument("--instances-dir", type=Path, default=eval_dir / "instances")
    parser.add_argument("--cache-dir", type=Path, default=eval_dir / "cache")
    parser.add_argument("--repos", type=_csv, default=None, help="comma-separated upstream repos (default: every repo with instances)")
    parser.add_argument("--strategies", type=_csv, default=list(STRATEGIES))
    parser.add_argument("--query-strategies", type=_csv, default=list(QUERY_STRATEGIES))
    parser.add_argument("--max-instances-per-repo", type=int, default=None)
    parser.add_argument("--max-chunks-per-repo", type=int, default=DEFAULT_MAX_CHUNKS_PER_REPO)
    parser.add_argument("--max-index-equivalents", type=int, default=DEFAULT_MAX_INDEX_EQUIVALENTS)
    parser.add_argument("--plan", action="store_true", help="count chunks and print the bill; embed nothing, write nothing")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, default=None)
    return parser


async def _amain(args: argparse.Namespace) -> int:
    def say(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    api = load_retrieval_api()

    factory = None
    engine = None
    if not args.plan:
        from repolace_shared.config import SharedSettings
        from repolace_shared.db.session import create_engine, create_session_factory

        try:
            database_url = SharedSettings().database_url
        except Exception:  # noqa: BLE001 -- a settings error can echo the environment; say only what is missing
            print("repolace-eval retrieval-eval: DATABASE_URL is not configured", file=sys.stderr)
            return 1
        engine = create_engine(database_url)
        factory = create_session_factory(engine)
    try:
        run = await run_eval(
            factory, api, instances_dir=args.instances_dir, cache_dir=args.cache_dir, repos=args.repos,
            strategies=args.strategies, query_strategies=args.query_strategies,
            max_instances_per_repo=args.max_instances_per_repo, max_chunks_per_repo=args.max_chunks_per_repo,
            max_index_equivalents=args.max_index_equivalents, plan_only=args.plan, progress=say,
        )
    finally:
        if engine is not None:
            await engine.dispose()

    text = to_json(run) if args.format == "json" else to_markdown(run)
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        return asyncio.run(_amain(args))
    except (RetrievalEvalError, SelectError, InstanceError) as exc:
        print(f"repolace-eval retrieval-eval: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
