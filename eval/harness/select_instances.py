"""Choose benchmark instances from SWE-bench Verified, and say why every other one was not.

**Run by the maintainer, with network and disk** (it pages a dataset and clones
repositories); the tests drive it with `httpx.MockTransport` and a local fixture
repository. It writes `eval/instances/<id>.json` (`shared.instances.dump_instance`),
a `<id>.gold.patch` sidecar per instance, and `eval/instances/MANIFEST.md`. Nothing
is written until selection finishes, and an existing file is never overwritten
without `--overwrite`: a later hand edit to an instance (the gold analysis flips
`targeted_p2p`) must survive a re-run of the selection.

**The filters decide, not the allowlist.** The allowlist below is a recollection
and only bounds which repositories are worth cloning. Every instance then has to
survive, in order (the first failure is its recorded reason):

1. the row is complete and its cells were not truncated by the dataset server;
2. its `FAIL_TO_PASS` is non-empty, its id and `base_commit` are well-formed;
3. the test patch touches only `verify.scoring.is_protected_path` files, adds or
   modifies (no delete, rename, binary), and no dependency-manifest name;
4. the gold patch touches no protected path, no `.git*` component and nothing
   under `.github/`, and deletes/renames nothing -- the agent's edit tool refuses
   those paths, so such an instance is unfixable and would inflate the
   denominator;
5. SWE-bench has an environment entry for its repo and version, and it can be
   built: Python >= 3.8, or no system packages (archived Debian apt repositories);
6. at `base_commit` the tree has no symlink or gitlink (the byte-identical export
   refuses both), both patches pass `git apply --check`, and every post-apply file
   is valid UTF-8 text.

**Patches are applied to an index, not a checkout.** In a throwaway
`clone --shared --no-checkout` of the cache, `read-tree <base_commit>` fills the
index and `git apply --cached` applies to it. No working tree exists, so no
clean/smudge filter, `eol` conversion or hook can run, no symlink can be followed,
and the post-apply bytes are read straight from the blobs (a `read_text()` of a
checked-out file would turn CRLF into LF and the overlay would then differ from the
commit). The cache itself is never written to, and every git call goes through the
hardened wrapper (`sanitized_git_env`, `UNTRUSTED_TREE_CONFIG_ARGS`).

**Selection is deterministic and spread across repositories**: candidates are
ordered by `sha256(seed:instance_id)`, then taken round-robin across repositories
(at most `--max-per-repo` each) until `--count` pass the expensive filters.
Instances never evaluated because the target or a repository's cap was reached are listed as such.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from harness import specgen
from harness.patches import ADDED, BINARY, MODIFIED, PatchError, files_in_patch
from repolace_shared.git.repo import (
    CLONE_TIMEOUT_SECONDS,
    EXPORT_TIMEOUT_SECONDS,
    GitCommandError,
    GitError,
    clone,
    run_git,
    run_git_bytes,
)
from repolace_shared.instances import (
    InstanceError,
    InstanceSpec,
    dump_instance,
    instance_path,
    load_instance,
)
from verify.dockerfile import MANIFEST_NAMES
from verify.scoring import is_protected_path

#: Pure-Python pytest repositories, from the maintainer's recollection of the
#: Verified mix -- the filters below, not this list, decide what is selected.
#: Excluded on purpose: `django/django` (its own runner), `sympy/sympy`
#: (`bin/test`), and `astropy/astropy`, `matplotlib/matplotlib`,
#: `scikit-learn/scikit-learn`, whose C extensions are built inside the tree and
#: then masked by the run-time `/repo` bind mount.
ALLOWED_REPOS = (
    "pytest-dev/pytest",
    "pylint-dev/pylint",
    "psf/requests",
    "pydata/xarray",
    "sphinx-doc/sphinx",
    "mwaskom/seaborn",
    "pallets/flask",
)

DATASET = "princeton-nlp/SWE-bench_Verified"
HF_ENDPOINT = "https://datasets-server.huggingface.co"
PAGE_SIZE = 100
DEFAULT_COUNT = 25
DEFAULT_MAX_PER_REPO = 6
DEFAULT_SEED = 0

_COLUMNS = (
    "instance_id", "repo", "base_commit", "version", "problem_statement",
    "patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS",
)
_BASE_COMMIT = re.compile(r"[0-9a-f]{40}")
_ISSUE_NUMBER = re.compile(r"-(\d+)$")
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_REGULAR_MODES = frozenset({"100644", "100755"})
_UNSUPPORTED_MODES = frozenset({"120000", "160000"})

GOLD_PATCH_SUFFIX = ".gold.patch"
MANIFEST_NAME = "MANIFEST.md"


class SelectError(RuntimeError):
    """The selection could not run (dataset unreachable, clone failed, files in the way)."""


class Rejected(Exception):
    """One instance failed a filter. `code` groups reasons in the manifest."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    @property
    def reason(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class Candidate:
    instance_id: str
    repo: str
    base_commit: str
    version: str
    problem_statement: str
    patch: str
    test_patch: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    issue_number: int
    #: The `RepoSpec` mapping from `specgen.spec_for`.
    spec: Mapping[str, Any]


@dataclass(frozen=True)
class Selected:
    candidate: Candidate
    instance: InstanceSpec


# --- the dataset -------------------------------------------------------------


@dataclass(frozen=True)
class RawRow:
    row_idx: int
    row: dict[str, Any]
    #: Columns the server cut short. Anything it cut is not the real content.
    truncated: tuple[str, ...]


async def _get_page(
    client: httpx.AsyncClient,
    endpoint: str,
    params: Mapping[str, Any],
    retries: int,
    sleep: Callable[[float], Awaitable[None]],
) -> dict[str, Any]:
    """One `rows` page, retrying transport errors, 429 and 5xx; anything else is final."""
    last = "no attempt made"
    for attempt in range(retries + 1):
        delay = float(min(2**attempt, 30))
        try:
            response = await client.get(f"{endpoint}/rows", params=dict(params))
        except httpx.TransportError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError:
                    last = "response is not JSON"
                else:
                    if isinstance(body, dict) and isinstance(body.get("rows"), list):
                        return body
                    last = "response has no 'rows' list"
            elif response.status_code in _RETRYABLE:
                last = f"HTTP {response.status_code}"
                retry_after = response.headers.get("retry-after", "")
                if retry_after.isdigit():
                    delay = min(float(retry_after), 60.0)
            else:
                raise SelectError(f"dataset server answered HTTP {response.status_code}: {response.text[:200]}")
        if attempt < retries:
            await sleep(delay)
    raise SelectError(f"dataset page {dict(params)} failed after {retries + 1} attempts: {last}")


def _raw_rows(body: Mapping[str, Any]) -> list[RawRow]:
    rows = []
    for item in body["rows"]:
        if not isinstance(item, dict) or not isinstance(item.get("row"), dict) or not isinstance(item.get("row_idx"), int):
            raise SelectError(f"unexpected row shape from the dataset server: {str(item)[:120]}")
        truncated = item.get("truncated_cells") or []
        rows.append(RawRow(item["row_idx"], item["row"], tuple(str(c) for c in truncated)))
    return rows


async def fetch_dataset_rows(
    client: httpx.AsyncClient,
    *,
    dataset: str = DATASET,
    config: str = "default",
    split: str = "test",
    endpoint: str = HF_ENDPOINT,
    page_size: int = PAGE_SIZE,
    retries: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[RawRow]:
    """Every row of the dataset, through the datasets-server `rows` endpoint.

    Fails if the rows received are not as many as the server says exist: a partial
    listing would silently shrink the candidate pool and make the manifest's
    rejection list look complete. A row whose cells the server truncated is
    re-requested alone (the cut is by response size); if it is still cut it is
    returned as truncated and rejected downstream, never used as if whole.
    """
    base = {"dataset": dataset, "config": config, "split": split}
    rows: list[RawRow] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        body = await _get_page(client, endpoint, {**base, "offset": offset, "length": page_size}, retries, sleep)
        if total is None:
            total = body.get("num_rows_total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise SelectError("the dataset server did not report num_rows_total")
        page = _raw_rows(body)
        if not page and offset < total:
            raise SelectError(f"the dataset server returned an empty page at offset {offset} of {total}")
        for raw in page:
            if set(raw.truncated) & set(_COLUMNS):
                retry = await _get_page(client, endpoint, {**base, "offset": raw.row_idx, "length": 1}, retries, sleep)
                again = _raw_rows(retry)
                if again and again[0].row_idx == raw.row_idx:
                    raw = again[0]
            rows.append(raw)
        offset += len(page)
    if len(rows) != total:
        raise SelectError(f"received {len(rows)} rows but the server reports {total}")
    return rows


# --- the cheap, pure filters -------------------------------------------------


def _string_list(value: object, column: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise Rejected("malformed-row", f"{column} is not valid JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise Rejected("malformed-row", f"{column} must be a list of strings")
    return tuple(value)


def patch_rejection(test_patch: str, patch: str) -> Rejected | None:
    """Filters 3 and 4: what the two patches touch. Pure.

    A test patch may only add or modify test infrastructure and touch no
    dependency manifest (the overlay is applied after the image is built, so a
    changed manifest would never be installed). A gold patch must be something the
    agent's own edit tool could produce.
    """
    try:
        test_changes = files_in_patch(test_patch)
        gold_changes = files_in_patch(patch)
    except PatchError as exc:
        return Rejected("unreadable-patch", str(exc))

    if not test_changes:
        return Rejected("test-patch", "it touches no file")
    for change in test_changes:
        if change.status == BINARY:
            return Rejected("test-patch", f"touches a binary file: {change.path}")
        if change.status not in (ADDED, MODIFIED):
            return Rejected("test-patch", f"{change.status} {change.path}: the overlay can only add or modify")
        if PurePosixPath(change.path).name in MANIFEST_NAMES:
            return Rejected("test-patch", f"touches a dependency manifest: {change.path}")
        if not is_protected_path(change.path):
            return Rejected("test-patch", f"touches a non-test file: {change.path}")

    if not gold_changes:
        return Rejected("gold-patch", "it touches no file")
    for change in gold_changes:
        if change.status == BINARY:
            return Rejected("gold-patch", f"touches a binary file: {change.path}")
        if change.status not in (ADDED, MODIFIED):
            return Rejected("gold-patch", f"{change.status} {change.path}: the agent cannot delete or rename")
        parts = PurePosixPath(change.path).parts
        if any(part.lower().startswith(".git") for part in parts):
            return Rejected("gold-patch", f"touches a .git* path the edit tool refuses: {change.path}")
        if is_protected_path(change.path):
            return Rejected("gold-patch", f"touches a protected (test, config or .github) path: {change.path}")
    return None


def parse_candidate(raw: RawRow, table: Mapping[str, Any]) -> Candidate:
    """Filters 1, 2, 3, 4 and 5 for one dataset row, or `Rejected`. No git, no network."""
    missing = [c for c in _COLUMNS if c not in raw.row]
    if missing:
        raise Rejected("malformed-row", f"missing column(s) {', '.join(missing)}")
    cut = sorted(set(raw.truncated) & set(_COLUMNS))
    if cut:
        raise Rejected("truncated-cell", f"the dataset server truncated {', '.join(cut)}")
    values = raw.row
    for column in ("instance_id", "repo", "base_commit", "version", "problem_statement", "patch", "test_patch"):
        if not isinstance(values[column], str):
            raise Rejected("malformed-row", f"{column} must be a string")

    instance_id = values["instance_id"]
    try:
        # The id is a file name, a container name and a branch name; it is validated
        # with the rule the instance files are held to, before anything uses it.
        instance_path(Path("."), instance_id)
    except InstanceError as exc:
        raise Rejected("bad-instance-id", str(exc)) from exc
    if not _BASE_COMMIT.fullmatch(values["base_commit"]):
        raise Rejected("bad-base-commit", repr(values["base_commit"]))
    number = _ISSUE_NUMBER.search(instance_id)
    if number is None or not 1 <= int(number[1]) <= 2**31 - 1:
        raise Rejected("bad-instance-id", f"{instance_id!r} has no usable trailing issue number")

    fail_to_pass = _string_list(values["FAIL_TO_PASS"], "FAIL_TO_PASS")
    if not fail_to_pass:
        raise Rejected("empty-fail-to-pass", "no test is expected to go from failing to passing")
    pass_to_pass = _string_list(values["PASS_TO_PASS"], "PASS_TO_PASS")

    rejection = patch_rejection(values["test_patch"], values["patch"])
    if rejection is not None:
        raise rejection

    try:
        spec = specgen.spec_for(values["repo"], values["version"], table)
    except specgen.SpecgenError as exc:
        raise Rejected("environment", str(exc)) from exc
    unbuildable = specgen.environment_rejection(spec)
    if unbuildable is not None:
        raise Rejected("environment", unbuildable)

    return Candidate(
        instance_id=instance_id,
        repo=values["repo"],
        base_commit=values["base_commit"],
        version=values["version"],
        problem_statement=values["problem_statement"],
        patch=values["patch"],
        test_patch=values["test_patch"],
        fail_to_pass=fail_to_pass,
        pass_to_pass=pass_to_pass,
        issue_number=int(number[1]),
        spec=spec,
    )


# --- the expensive filters: clones and git -----------------------------------


def cache_path(cache_dir: Path, repo: str) -> Path:
    """`eval/cache/<owner>__<name>`. `repo` is from the allowlist, so it is a safe name."""
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name or repo not in ALLOWED_REPOS:
        raise SelectError(f"refusing a cache path for {repo!r}: not an allowlisted owner/name")
    return Path(cache_dir) / f"{owner}__{name}"


def github_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


class CloneCache:
    """Full (non-shallow) clones of the upstream repositories, one per repo, kept between runs."""

    def __init__(self, cache_dir: Path, url_for: Callable[[str], str] = github_url) -> None:
        self.cache_dir = Path(cache_dir)
        self.url_for = url_for
        self._fetched: set[str] = set()

    async def get(self, repo: str) -> Path:
        destination = cache_path(self.cache_dir, repo)
        if (destination / ".git").is_dir():
            return destination
        url = self.url_for(repo)
        listing = await run_git("ls-remote", "--symref", url, "HEAD", timeout=CLONE_TIMEOUT_SECONDS)
        match = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", listing, re.MULTILINE)
        if match is None:
            raise SelectError(f"cannot determine the default branch of {url}")
        # Cloned beside the final name and renamed, so an interrupted clone cannot
        # leave a half-populated directory that every later run would trust.
        partial = destination.with_name(destination.name + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await clone(url, partial, match[1])
        os.replace(partial, destination)
        return destination

    async def ensure_commit(self, repo: str, sha: str) -> Path:
        """The cache clone for `repo`, guaranteed to hold `sha`, fetching once if it does not."""
        destination = await self.get(repo)
        if await _has_commit(destination, sha):
            return destination
        if repo not in self._fetched:
            self._fetched.add(repo)
            await run_git("fetch", "--quiet", "--tags", "origin", cwd=destination, timeout=CLONE_TIMEOUT_SECONDS)
            if await _has_commit(destination, sha):
                return destination
        raise Rejected("base-commit-missing", f"{sha} is not in the clone of {repo}")


async def _has_commit(repo: Path, sha: str) -> bool:
    try:
        await run_git("cat-file", "-e", f"{sha}^{{commit}}", cwd=repo)
    except GitCommandError:
        return False
    return True


async def _index_entries(work: Path) -> list[tuple[str, str, str]]:
    """`(mode, blob sha, path)` for every index entry, via `-z` so no path is quoted."""
    raw = await run_git_bytes("ls-files", "-s", "-z", cwd=work, timeout=EXPORT_TIMEOUT_SECONDS)
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode, sha, _stage = meta.decode("ascii").split(" ")
        entries.append((mode, sha, path.decode("utf-8", errors="surrogateescape")))
    return entries


async def _apply(work: Path, patch_file: Path, label: str, *, check_only: bool) -> None:
    args = ["apply", "--cached", "--whitespace=nowarn"]
    if check_only:
        args.append("--check")
    try:
        await run_git(*args, str(patch_file), cwd=work)
    except GitCommandError as exc:
        raise Rejected("apply-failed", f"{label} does not apply at the base commit: {exc.stderr[:300]}") from exc


async def _post_apply_files(work: Path, patch_file: Path, label: str, paths: Sequence[str]) -> dict[str, str]:
    """Apply to the index and read the resulting blobs as UTF-8 text, byte for byte."""
    await _apply(work, patch_file, label, check_only=False)
    by_path = {path: (mode, sha) for mode, sha, path in await _index_entries(work)}
    files: dict[str, str] = {}
    for path in paths:
        if path not in by_path:
            raise Rejected("apply-failed", f"{label}: {path} is not in the tree after applying it")
        mode, sha = by_path[path]
        if mode not in _REGULAR_MODES:
            raise Rejected("non-regular-file", f"{label} leaves {path} as mode {mode}")
        blob = await run_git_bytes("cat-file", "blob", sha, cwd=work)
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Rejected("not-utf8", f"{label}: {path} is not valid UTF-8") from exc
        if "\x00" in text:
            raise Rejected("not-utf8", f"{label}: {path} contains a NUL byte, so it is not text")
        files[path] = text
    return files


async def analyse_in_git(candidate: Candidate, caches: CloneCache) -> tuple[dict[str, str], dict[str, str]]:
    """Filter 6. Returns `(test_files, gold_files)`: path -> full post-apply text."""
    cache = await caches.ensure_commit(candidate.repo, candidate.base_commit)
    test_paths = [c.path for c in files_in_patch(candidate.test_patch)]
    gold_paths = [c.path for c in files_in_patch(candidate.patch)]

    with tempfile.TemporaryDirectory(prefix="repolace-select-") as scratch:
        scratch_dir = Path(scratch)
        work = scratch_dir / "work"
        # `--shared`: objects are borrowed from the cache, so nothing is copied and
        # nothing is written into it; `--no-checkout`: there is no working tree.
        await run_git("clone", "--shared", "--no-checkout", "--quiet", str(cache), str(work))
        await run_git("read-tree", candidate.base_commit, cwd=work)

        unsupported = [path for mode, _, path in await _index_entries(work) if mode in _UNSUPPORTED_MODES]
        if unsupported:
            shown = ", ".join(unsupported[:5]) + (" ..." if len(unsupported) > 5 else "")
            raise Rejected(
                "symlink-or-submodule",
                f"{len(unsupported)} symlink/gitlink entr{'y' if len(unsupported) == 1 else 'ies'} at the base commit: {shown}",
            )

        patch_files = {}
        for label, text in (("test_patch", candidate.test_patch), ("patch", candidate.patch)):
            target = scratch_dir / f"{label}.diff"
            target.write_bytes(text.encode("utf-8"))
            patch_files[label] = target
            await _apply(work, target, label, check_only=True)

        test_files = await _post_apply_files(work, patch_files["test_patch"], "test_patch", test_paths)
        # Back to the base tree: the gold patch is a fix to the base, not to the
        # tests, and the two must not be applied on top of each other.
        await run_git("read-tree", candidate.base_commit, cwd=work)
        gold_files = await _post_apply_files(work, patch_files["patch"], "patch", gold_paths)
    return test_files, gold_files


def build_instance(candidate: Candidate, test_files: Mapping[str, str], gold_files: Mapping[str, str]) -> InstanceSpec:
    """The instance, validated by writing it out and reading it back with the loader.

    `targeted_p2p` is always False and there are no `test_targets`: the gold
    analysis (a later stage) proposes the flip, never this selection.
    """
    instance = InstanceSpec(
        instance_id=candidate.instance_id,
        repo=candidate.repo,
        base_commit=candidate.base_commit,
        version=candidate.version,
        problem_statement=candidate.problem_statement,
        issue_number=candidate.issue_number,
        fail_to_pass=candidate.fail_to_pass,
        pass_to_pass=candidate.pass_to_pass,
        test_files=dict(test_files),
        gold_files=dict(gold_files),
        spec=dict(candidate.spec),
        targeted_p2p=False,
    )
    with tempfile.TemporaryDirectory(prefix="repolace-select-") as staging:
        staged = Path(staging) / f"{candidate.instance_id}.json"
        try:
            dump_instance(instance, staged)
            load_instance(staged)
        except InstanceError as exc:
            raise Rejected("invalid-instance", str(exc)) from exc
    return instance


# --- selection ---------------------------------------------------------------


def order_key(seed: int, instance_id: str) -> str:
    """Deterministic and stable across Python versions, unlike `random.shuffle`."""
    return hashlib.sha256(f"{seed}:{instance_id}".encode()).hexdigest()


@dataclass
class SelectionResult:
    selected: list[Selected]
    rejections: dict[str, str]
    not_evaluated: list[str]


async def choose(
    pool: Sequence[Candidate],
    evaluate: Callable[[Candidate], Awaitable[Selected]],
    *,
    count: int,
    max_per_repo: int,
    seed: int,
    progress: Callable[[str], None] = lambda _: None,
) -> SelectionResult:
    """Round-robin over repositories in seeded order until `count` instances pass `evaluate`.

    A rejected instance does not use up its repository's cap, only an accepted one
    does, so one repository whose instances mostly fail the git filters is not
    starved out by its own rejects.
    """
    queues: dict[str, list[Candidate]] = {}
    for candidate in sorted(pool, key=lambda c: order_key(seed, c.instance_id)):
        queues.setdefault(candidate.repo, []).append(candidate)

    selected: list[Selected] = []
    per_repo: Counter[str] = Counter()
    rejections: dict[str, str] = {}

    def open_repos() -> list[str]:
        return [repo for repo in sorted(queues) if queues[repo] and per_repo[repo] < max_per_repo]

    while len(selected) < count and open_repos():
        for repo in open_repos():
            if len(selected) >= count:
                break
            candidate = queues[repo].pop(0)
            progress(f"evaluating {candidate.instance_id}")
            try:
                selected.append(await evaluate(candidate))
            except Rejected as exc:
                rejections[candidate.instance_id] = exc.reason
                progress(f"  rejected: {exc.reason}")
            else:
                per_repo[repo] += 1
    leftover = sorted(c.instance_id for remaining in queues.values() for c in remaining)
    return SelectionResult(selected, rejections, leftover)


# --- the manifest and the files ----------------------------------------------


def _write_atomic(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def render_manifest(
    *,
    dataset: str,
    seed: int,
    count: int,
    max_per_repo: int,
    total_rows: int,
    off_allowlist: Mapping[str, int],
    pre_rejections: Mapping[str, str],
    result: SelectionResult,
) -> str:
    """Every candidate, every rejection with its reason, and every instance not evaluated.

    Deterministic: no timestamps, sorted throughout, so a re-run with the same
    inputs produces the same bytes and a diff shows a real change.
    """
    rejections = {**pre_rejections, **result.rejections}
    out = [
        "# Instance manifest",
        "",
        "Written by `repolace-eval select`. The filter is auditable from this file alone: every candidate, "
        "and every instance that was rejected with the first reason it failed.",
        "",
        f"- dataset: `{dataset}` ({total_rows} rows)",
        f"- seed: {seed}; target: {count} candidates; at most {max_per_repo} per repository",
        f"- selected: {len(result.selected)}; rejected: {len(rejections)}; "
        f"not evaluated (target or per-repo cap reached): {len(result.not_evaluated)}",
        "",
        "## Selected",
        "",
        "| instance | repo | version | base image | FAIL_TO_PASS | PASS_TO_PASS | test files | gold files | system packages |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in sorted(result.selected, key=lambda s: s.instance.instance_id):
        i = item.instance
        spec = i.spec
        out.append(
            f"| {i.instance_id} | {i.repo} | {i.version} | {spec['base_image']} | {len(i.fail_to_pass)} | "
            f"{len(i.pass_to_pass)} | {len(i.test_files)} | {len(i.gold_files)} | "
            f"{'yes' if specgen.needs_system_packages(spec) else 'no'} |"
        )
    out += ["", "## Rejected, by reason", ""]
    by_code = Counter(reason.split(":", 1)[0] for reason in rejections.values())
    for code, n in sorted(by_code.items()):
        out.append(f"- `{code}`: {n}")
    out += ["", "## Every rejection", "", "| instance | reason |", "|---|---|"]
    for instance_id in sorted(rejections):
        out.append(f"| {instance_id} | {rejections[instance_id].replace('|', chr(92) + '|')} |")
    out += ["", "## Not evaluated", ""]
    if result.not_evaluated:
        out.append(
            "These passed every cheap filter and were not run through the git filters because the target "
            "or their repository's cap was reached: " + ", ".join(result.not_evaluated)
        )
    else:
        out.append("none")
    out += [
        "",
        "## Excluded by repository",
        "",
        "Rows outside the allowlist are counted, not listed (the exclusion is by design, see "
        "`ALLOWED_REPOS`): "
        + (", ".join(f"{repo} ({n})" for repo, n in sorted(off_allowlist.items())) or "none")
        + ".",
        "",
    ]
    return "\n".join(out)


@dataclass(frozen=True)
class Options:
    instances_dir: Path
    cache_dir: Path
    specs_path: Path
    dataset: str = DATASET
    config: str = "default"
    split: str = "test"
    endpoint: str = HF_ENDPOINT
    count: int = DEFAULT_COUNT
    max_per_repo: int = DEFAULT_MAX_PER_REPO
    seed: int = DEFAULT_SEED
    overwrite: bool = False


async def run_select(
    options: Options,
    client: httpx.AsyncClient,
    *,
    url_for: Callable[[str], str] = github_url,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    progress: Callable[[str], None] = lambda _: None,
) -> SelectionResult:
    """Fetch, filter, choose and write. Returns the selection."""
    table = specgen.load_swebench_specs(options.specs_path)
    rows = await fetch_dataset_rows(
        client, dataset=options.dataset, config=options.config, split=options.split,
        endpoint=options.endpoint, sleep=sleep,
    )

    ids = [r.row.get("instance_id") for r in rows]
    duplicates = sorted({i for i in ids if isinstance(i, str) and ids.count(i) > 1})
    if duplicates:
        raise SelectError(f"the dataset repeats instance id(s): {', '.join(duplicates[:5])}")

    pool: list[Candidate] = []
    pre_rejections: dict[str, str] = {}
    off_allowlist: Counter[str] = Counter()
    for raw in rows:
        repo = raw.row.get("repo")
        if repo not in ALLOWED_REPOS:
            off_allowlist[str(repo)] += 1
            continue
        try:
            pool.append(parse_candidate(raw, table))
        except Rejected as exc:
            pre_rejections[str(raw.row.get("instance_id"))] = exc.reason
    progress(f"{len(rows)} rows; {sum(off_allowlist.values())} outside the allowlist; "
             f"{len(pre_rejections)} rejected by cheap filters; {len(pool)} to evaluate")

    caches = CloneCache(options.cache_dir, url_for)

    async def evaluate(candidate: Candidate) -> Selected:
        test_files, gold_files = await analyse_in_git(candidate, caches)
        return Selected(candidate, build_instance(candidate, test_files, gold_files))

    result = await choose(
        pool, evaluate, count=options.count, max_per_repo=options.max_per_repo, seed=options.seed,
        progress=progress,
    )

    directory = options.instances_dir
    directory.mkdir(parents=True, exist_ok=True)
    targets = [directory / MANIFEST_NAME]
    for item in result.selected:
        targets.append(instance_path(directory, item.candidate.instance_id))
        targets.append(directory / f"{item.candidate.instance_id}{GOLD_PATCH_SUFFIX}")
    existing = [str(t) for t in targets if t.exists()]
    if existing and not options.overwrite:
        raise SelectError(
            f"refusing to overwrite {len(existing)} existing file(s) (a re-run would discard hand edits to "
            f"instances); pass --overwrite or use another --instances-dir: {', '.join(existing[:3])}"
        )

    for item in result.selected:
        dump_instance(item.instance, instance_path(directory, item.candidate.instance_id))
        _write_atomic(directory / f"{item.candidate.instance_id}{GOLD_PATCH_SUFFIX}", item.candidate.patch)
    _write_atomic(
        directory / MANIFEST_NAME,
        render_manifest(
            dataset=options.dataset, seed=options.seed, count=options.count, max_per_repo=options.max_per_repo,
            total_rows=len(rows), off_allowlist=off_allowlist, pre_rejections=pre_rejections, result=result,
        ),
    )
    return result


def _default_eval_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    eval_dir = _default_eval_dir()
    parser = argparse.ArgumentParser(prog="repolace-eval select", description="Choose benchmark instances from SWE-bench Verified.")
    parser.add_argument("--instances-dir", type=Path, default=eval_dir / "instances")
    parser.add_argument("--cache-dir", type=Path, default=eval_dir / "cache")
    parser.add_argument("--specs", type=Path, default=specgen.DEFAULT_SPECS_PATH, help="the vendored SWE-bench spec snapshot")
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--config", default="default")
    parser.add_argument("--split", default="test")
    parser.add_argument("--endpoint", default=HF_ENDPOINT)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--max-per-repo", type=int, default=DEFAULT_MAX_PER_REPO)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--overwrite", action="store_true", help="replace existing instance files and the manifest")
    return parser


async def _amain(options: Options) -> int:
    def say(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        result = await run_select(options, client, progress=say)
    print(f"selected {len(result.selected)} instance(s); rejected {len(result.rejections)}; "
          f"manifest: {options.instances_dir / MANIFEST_NAME}")
    return 0 if result.selected else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.count < 1 or args.max_per_repo < 1:
        print("repolace-eval select: --count and --max-per-repo must be at least 1", file=sys.stderr)
        return 2
    options = Options(
        instances_dir=args.instances_dir, cache_dir=args.cache_dir, specs_path=args.specs, dataset=args.dataset,
        config=args.config, split=args.split, endpoint=args.endpoint, count=args.count,
        max_per_repo=args.max_per_repo, seed=args.seed, overwrite=args.overwrite,
    )
    try:
        return asyncio.run(_amain(options))
    except (SelectError, specgen.SpecgenError, GitError, InstanceError) as exc:
        print(f"repolace-eval select: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
