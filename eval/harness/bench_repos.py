"""Create, populate and delete the benchmark repositories (`repolace-eval fork`).

User-run. This is the only code in the project that holds a GitHub personal
access token, and the one place that creates and deletes real repositories.

**Why the guardrails are the whole design.** Benchmark repositories live in the
existing GitHub organisation `repolace`, which also hosts the product repository
`repolace/repolace`. A dedicated organisation would have made it impossible for
this script to touch the product repo; here the only thing that does is the name
check below. So that check is small, central and obviously correct, and a test
tries to defeat it with every shape of hostile name.

* **Only `bench-[A-Za-z0-9_.-]+` is ever touched** -- create, push, settings,
  installation changes and delete alike. `check_bench_name` is the one decision;
  `BenchRepo` cannot be built without passing it; and `_request` refuses any
  endpoint outside a short allow-list whose repository segment is the same
  pattern, so a future method that builds a path from a raw string still cannot
  reach `repolace/repolace`. `fullmatch`, not `$`: `$` also matches before a
  trailing newline.
* **Each repository is created private, with issues and wiki off, and Actions
  are disabled before anything is pushed.** An agent-authored workflow on a PR
  (or a workflow already in the upstream tree, run by the push of the base
  commit) would otherwise run with that repository's own token. If disabling
  Actions fails the repository is **not ready**: it is not recorded in
  `bench_repos.toml` and the failure is reported.
* **The token is read from `REPOLACE_BENCH_GITHUB_TOKEN`, here and nowhere
  else.** It is never logged, never in argv, never in an exception message
  (`_scrub` removes the literal value and anything shaped like a credential), and
  never given to the pipeline: `runner` strips the variable from the children it
  starts. Git authenticates through the per-command credential helper that
  `repolace_shared.git.repo` already defines -- the token travels in an
  environment variable the helper reads, and only for `https` + the expected host.
  The base URLs are keyword arguments of `main`, not flags, so nothing on the
  command line can redirect the token to another host.
* **`--delete` removes only repositories listed in `bench_repos.toml`**, and the
  file is validated on read: an entry that is not exactly
  `repolace/bench-<instance_id>` is an error, so a hand-edited file cannot point
  `--delete` (or `enqueue`) at another repository.

**Token scopes (minimal; check GitHub's current documentation).** A classic
personal access token with `repo` covers creating private repositories in the
organisation (you must be a member who may create repositories), pushing, and
`PUT .../actions/permissions`. `--delete` additionally needs `delete_repo`.
`--add-to-installation` calls `PUT /user/installations/{id}/repositories/{id}`,
which needs the authenticated user to have admin access to the repository (an
organisation owner has). The token can reach every repository its user can,
including `repolace/repolace` -- that is exactly why the guards above exist and
why you should **set the shortest expiry that covers the run and revoke the token
as soon as it is done.**

`--add-to-installation` is off by default. Whether the App is installed on "all
repositories" or on "selected repositories" is the maintainer's choice at a later
checkpoint; with "selected" this flag is what makes each `bench-*` repository
visible to the App without widening its access to the product repo.

Exit codes: 0 every repository ready; 1 at least one is not ready or a call
failed; 2 bad invocation (no token, unreadable instances, unlisted delete).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from repolace_shared.git.repo import (
    CLONE_TIMEOUT_SECONDS,
    DEFAULT_CREDENTIAL_HOST,
    GitError,
    GitRepo,
    redact,
    run_git,
)
from repolace_shared.instances import InstanceError, InstanceSpec, load_instances
from repolace_shared.paths import PathEscapesRoot, resolve_within

#: The organisation benchmark repositories live in. A constant, not a flag.
BENCH_OWNER = "repolace"
TOKEN_ENV_VAR = "REPOLACE_BENCH_GITHUB_TOKEN"
GITHUB_API = "https://api.github.com"
GITHUB_GIT = "https://github.com"

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INSTANCES_DIR = _REPO_ROOT / "eval" / "instances"
DEFAULT_CACHE_DIR = _REPO_ROOT / "eval" / "cache"
MAPPING_FILENAME = "bench_repos.toml"

#: What `bench-<instance_id>` may look like, as strict as the instance-id rule in
#: `repolace_shared.instances` (a letter or digit first, no `..`, no trailing `.`,
#: no trailing `.git`, which GitHub strips from a repository name) and bounded in
#: length. One source string, used by the guard and by the request allow-list.
_BENCH_NAME_SOURCE = r"bench-[A-Za-z0-9](?:[A-Za-z0-9_-]|\.(?!\.)){0,90}(?<!\.)(?<!\.[Gg][Ii][Tt])"
_BENCH_NAME = re.compile(_BENCH_NAME_SOURCE)
#: `owner/name`, each part starting with a letter, digit or underscore, so neither can be
#: `.` or `..` and the pair cannot reach outside `eval/cache` or the clone URL.
_UPSTREAM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*")

#: The only requests this module may send: (method, whole path). The repository
#: segment is the bench pattern, so this is the second place the name rule is
#: enforced, at the point where a request actually leaves the process.
_REPO_PATH = "/repos/" + BENCH_OWNER + "/" + _BENCH_NAME_SOURCE
_ALLOWED_REQUESTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(rf"/orgs/{BENCH_OWNER}/repos")),
    ("GET", re.compile(_REPO_PATH)),
    ("DELETE", re.compile(_REPO_PATH)),
    ("PUT", re.compile(_REPO_PATH + "/actions/permissions")),
    ("PUT", re.compile(r"/user/installations/[0-9]+/repositories/[0-9]+")),
)

#: The description `create_repo` gives every repository it creates. It is the only
#: evidence the tool has that a repository is its own, so it is what a 422 "already
#: exists" and `--delete` are checked against: a repository without it was made by
#: someone else and is never adopted or deleted. One constant, used in both places.
BENCH_MARKER = "repolace benchmark instance. Not a real project."

#: The one ref the base commit is pushed to, and the only refspec shape allowed.
_REFSPEC = re.compile(r"[0-9a-f]{40}:refs/heads/main")


class BenchRepoError(RuntimeError):
    """Something this module refuses to do, or a GitHub call that failed."""


class NotCreatedByThisTool(BenchRepoError):
    """A repository exists under a bench- name but does not carry this tool's marker."""


# --- the name guard -------------------------------------------------------------


def check_bench_name(name: object) -> str:
    """The one decision about which repositories this module may touch.

    Case-sensitive on purpose (`BENCH-x` is refused), whole-string (`fullmatch`:
    trailing whitespace and a trailing newline are refused), ASCII-only, and no
    `/`, `%`, `\\` or whitespace, so no path trick survives it. As strict as the
    instance-id rule: a name GitHub would normalise (`bench-x.git`, `bench-x.`) or
    that has a `..` in it is refused, so what is checked is what is sent.
    """
    if not isinstance(name, str) or _BENCH_NAME.fullmatch(name) is None:
        raise BenchRepoError(
            f"refusing repository name {name!r}: only bench-<letter or digit>[A-Za-z0-9_-.]* names "
            f"(no '..', no trailing '.' or '.git', at most 97 characters) are ever touched"
        )
    return name


@dataclass(frozen=True)
class BenchRepo:
    """A repository in `BENCH_OWNER` that has passed `check_bench_name`.

    Every GitHub method takes this rather than a string, so the guard cannot be
    skipped by a caller that forgot it.
    """

    name: str

    def __post_init__(self) -> None:
        check_bench_name(self.name)

    @property
    def full_name(self) -> str:
        return f"{BENCH_OWNER}/{self.name}"

    @classmethod
    def for_instance(cls, instance_id: str) -> BenchRepo:
        return cls(f"bench-{instance_id}")

    @classmethod
    def from_full_name(cls, full_name: object) -> BenchRepo:
        if not isinstance(full_name, str):
            raise BenchRepoError(f"refusing repository {full_name!r}: not a string")
        owner, separator, name = full_name.partition("/")
        if not separator or owner != BENCH_OWNER:
            raise BenchRepoError(f"refusing repository {full_name!r}: it is not in {BENCH_OWNER}/bench-*")
        return cls(name)


# --- the mapping file -----------------------------------------------------------


@dataclass(frozen=True)
class BenchEntry:
    repo: str
    #: "ready": private, Actions disabled, base commit pushed. "incomplete": the
    #: repository exists on GitHub and is this tool's, but a later step failed. Only
    #: so `--delete` can list and remove it; nothing else may use it.
    status: str = "ready"


_STATUSES = ("ready", "incomplete")


def load_bench_entries(path: Path) -> dict[str, BenchEntry]:
    """Every entry in the mapping file, validated; `{}` if the file is absent.

    An entry is `"<id>" = "repolace/bench-<id>"` (ready) or
    `"<id>" = { repo = "repolace/bench-<id>", status = "incomplete" }`. Each must
    be *exactly* the name derived from its instance id: the file is
    operator-edited, and an entry pointing anywhere else would otherwise reach
    `--delete` or become a task's repository.
    """
    if not path.exists():
        return {}
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BenchRepoError(f"{path}: cannot read: {type(exc).__name__}: {exc}") from exc
    entries: dict[str, BenchEntry] = {}
    for instance_id, value in document.items():
        if isinstance(value, dict):
            if set(value) != {"repo", "status"} or value["status"] not in _STATUSES:
                raise BenchRepoError(
                    f"{path}: entry {instance_id!r} must have exactly repo and a status of {' or '.join(_STATUSES)}"
                )
            full_name, status = value["repo"], value["status"]
        else:
            full_name, status = value, "ready"
        try:
            repo = BenchRepo.from_full_name(full_name)
        except BenchRepoError as exc:
            raise BenchRepoError(f"{path}: entry {instance_id!r}: {exc}") from None
        if repo != BenchRepo.for_instance(instance_id):
            raise BenchRepoError(
                f"{path}: entry {instance_id!r} maps to {full_name!r}, expected {BenchRepo.for_instance(instance_id).full_name!r}"
            )
        entries[instance_id] = BenchEntry(full_name, status)
    return entries


def load_bench_repos(path: Path) -> dict[str, str]:
    """`instance_id -> "repolace/bench-<instance_id>"` for the READY entries only.

    Shared with `enqueue`: an incomplete repository is not one a task may use.
    """
    return {i: entry.repo for i, entry in load_bench_entries(path).items() if entry.status == "ready"}


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` through a temporary file in the same directory and a rename.

    A crash or an interrupt mid-write leaves the previous file or none, never a
    truncated one: these files are records of what exists (repositories, a run's
    manifest, a validation report) and a half-written one reads as a different
    truth. Shared by `enqueue` and `gold`; mode 0644.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _write_bench_repos(path: Path, entries: Mapping[str, BenchEntry]) -> None:
    lines = [
        "# Written by `repolace-eval fork`. instance_id -> benchmark repository.",
        "# A plain string is a ready repository: private, Actions disabled, base commit pushed.",
        "# An inline table with status = \"incomplete\" is one this tool created that a later step",
        "# failed on; it is listed so `fork --delete` can remove it, and nothing else may use it.",
    ]
    # `json.dumps` of a plain string is a valid TOML basic string; keys are quoted
    # because an instance id may contain '.', which a bare TOML key would split.
    for key in sorted(entries):
        entry = entries[key]
        if entry.status == "ready":
            lines.append(f"{json.dumps(key)} = {json.dumps(entry.repo)}")
        else:
            lines.append(f"{json.dumps(key)} = {{ repo = {json.dumps(entry.repo)}, status = {json.dumps(entry.status)} }}")
    atomic_write_text(path, "\n".join(lines) + "\n")


# --- the GitHub client ----------------------------------------------------------


@dataclass(frozen=True)
class RepoInfo:
    repo_id: int
    #: What GitHub says the repository's description is; compared with `BENCH_MARKER`.
    description: str | None = None


def _scrub(text: str, token: str) -> str:
    """Remove the literal token and anything shaped like a credential.

    The literal replacement is what makes this independent of GitHub's token
    formats; `redact` is the pattern-based second layer.
    """
    if token:
        text = text.replace(token, "<token>")
    return redact(text)


class GithubBench:
    """The handful of GitHub REST calls this module needs, behind the allow-list."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = GITHUB_API,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "repolace-bench-repos",
            },
            timeout=30.0,
            # A renamed repository answers 301 to its new name. Following it would
            # let GitHub, not this module, decide which repository a call lands on.
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def scrub(self, text: str) -> str:
        return _scrub(text, self._token)

    async def _request(self, method: str, path: str, *, body: Mapping[str, object] | None = None) -> httpx.Response:
        if not any(method == allowed and pattern.fullmatch(path) for allowed, pattern in _ALLOWED_REQUESTS):
            raise BenchRepoError(f"refusing {method} {path}: not an endpoint this module may call")
        try:
            return await self._http.request(method, path, json=body)
        except httpx.HTTPError as exc:
            # `from None`: the chained exception's text is not scrubbed.
            raise BenchRepoError(self.scrub(f"{method} {path} failed: {type(exc).__name__}: {exc}")) from None

    def _api_error(self, action: str, response: httpx.Response) -> BenchRepoError:
        # Scrub the whole body, then cut: scrubbing a body already cut at 300
        # characters leaves the first part of a token that straddles the cut.
        body = self.scrub(response.text)[:300]
        return BenchRepoError(self.scrub(f"{action}: GitHub answered {response.status_code}: {body}"))

    def _info(self, repo: BenchRepo, response: httpx.Response) -> RepoInfo:
        """Read a repository object, refusing one that is not the one asked for or not private."""
        try:
            data = response.json()
            repo_id, full_name, private = data["id"], data["full_name"], data["private"]
        except (ValueError, KeyError, TypeError):
            raise self._api_error(f"{repo.full_name}: unreadable repository object", response) from None
        if full_name != repo.full_name or isinstance(repo_id, bool) or not isinstance(repo_id, int):
            raise BenchRepoError(f"asked for {repo.full_name} but GitHub returned {full_name!r}")
        if private is not True:
            raise BenchRepoError(f"{repo.full_name} is not private; benchmark repositories must be")
        description = data.get("description")
        return RepoInfo(repo_id=repo_id, description=description if isinstance(description, str) else None)

    async def get_repo(self, repo: BenchRepo, *, missing_ok: bool = False) -> RepoInfo | None:
        """The repository, private-checked; None for a 404 when `missing_ok`."""
        response = await self._request("GET", f"/repos/{repo.full_name}")
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code != 200:
            raise self._api_error(f"get {repo.full_name}", response)
        return self._info(repo, response)

    @staticmethod
    def _require_marker(repo: BenchRepo, info: RepoInfo) -> RepoInfo:
        """Refuse a repository this tool did not create.

        The marker is the only evidence of ownership: without the check, a 422
        "already exists" would adopt, lock down, push to, and later delete a
        repository somebody else made under a bench- name.
        """
        if info.description != BENCH_MARKER:
            raise NotCreatedByThisTool(f"{repo.full_name} exists but was not created by this tool")
        return info

    async def get_owned_repo(self, repo: BenchRepo) -> RepoInfo:
        info = await self.get_repo(repo)
        assert info is not None
        return self._require_marker(repo, info)

    async def create_repo(self, repo: BenchRepo) -> tuple[RepoInfo, bool]:
        """Create the repository; `(info, True)`, or `(info, False)` when it already exists.

        "Already exists" is a 422 whose errors say so. Any other 422 is a real
        validation failure and stays an error: treating every 422 as a skip would
        report a rejected name as success.
        """
        response = await self._request(
            "POST",
            f"/orgs/{BENCH_OWNER}/repos",
            body={
                "name": repo.name,
                "description": BENCH_MARKER,
                "private": True,
                "has_issues": False,
                "has_wiki": False,
                "has_projects": False,
                "auto_init": False,
            },
        )
        if response.status_code == 201:
            return self._info(repo, response), True
        if response.status_code == 422 and _says_already_exists(response):
            return await self.get_owned_repo(repo), False
        raise self._api_error(f"create {repo.full_name}", response)

    async def disable_actions(self, repo: BenchRepo) -> None:
        response = await self._request("PUT", f"/repos/{repo.full_name}/actions/permissions", body={"enabled": False})
        if response.status_code != 204:
            raise self._api_error(f"disable Actions on {repo.full_name}", response)

    async def delete_repo(self, repo: BenchRepo) -> bool:
        """Delete it; False when it was already gone (so a re-run of `--delete` is idempotent).

        Looks the repository up first and refuses one without this tool's marker
        or one that is not private, **at the moment of the DELETE**: a listing in
        `bench_repos.toml` is a claim made earlier, and the repository may have
        been replaced by someone else's since.
        """
        info = await self.get_repo(repo, missing_ok=True)
        if info is None:
            return False
        self._require_marker(repo, info)
        response = await self._request("DELETE", f"/repos/{repo.full_name}")
        if response.status_code == 204:
            return True
        if response.status_code == 404:
            return False
        raise self._api_error(f"delete {repo.full_name}", response)

    async def add_to_installation(self, installation_id: int, repo: BenchRepo) -> None:
        # The numeric id comes from a verified `get_owned_repo` of this bench repository,
        # so the installation change cannot be aimed at another one.
        info = await self.get_owned_repo(repo)
        response = await self._request(
            "PUT", f"/user/installations/{installation_id}/repositories/{info.repo_id}"
        )
        if response.status_code != 204:
            raise self._api_error(f"add {repo.full_name} to installation {installation_id}", response)


def _says_already_exists(response: httpx.Response) -> bool:
    try:
        errors = response.json().get("errors", [])
        return any("already exists" in str(error.get("message", "")) for error in errors)
    except (ValueError, AttributeError, TypeError):
        return False


# --- git ------------------------------------------------------------------------

GitPush = Callable[[str, str, Path, str], Awaitable[None]]
"""`(remote_url, refspec, checkout, token) -> None`; raises `GitError` on failure."""

Checkout = Callable[[InstanceSpec, Path], contextlib.AbstractAsyncContextManager[Path]]
"""`(spec, cache_dir) -> async context manager yielding a full upstream checkout`."""


def check_push_target(url: str, refspec: str) -> None:
    """Re-check, where the push leaves the process, that it can only go where it should.

    The repository must be a `repolace/bench-*` one (`BenchRepo.from_full_name`,
    the same rule every API call is held to), the URL must carry no credentials,
    and the refspec must be exactly `<40-hex sha>:refs/heads/main`: one commit to
    one branch. A wider refspec (`--all`, tags, a later upstream branch) would put
    the commit that fixes the issue into the benchmark repository, which is an
    oracle leak. `file:` is accepted for the tests' local remotes, where only the
    last two path segments name the repository; it is reachable only through the
    `git_url` seam, never from the command line.
    """
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if parts.scheme == "https":
        if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname or len(segments) != 2:
            raise BenchRepoError(f"refusing to push to {parts.scheme}://{parts.hostname}{parts.path}: not a plain owner/repo URL")
        named = segments
    elif parts.scheme == "file" and len(segments) >= 2:
        named = segments[-2:]
    else:
        raise BenchRepoError(f"refusing to push to a {parts.scheme or 'schemeless'!r} URL")
    if not named[1].endswith(".git"):
        raise BenchRepoError("refusing to push: the repository URL must end in .git")
    BenchRepo.from_full_name(f"{named[0]}/{named[1][: -len('.git')]}")
    if _REFSPEC.fullmatch(refspec) is None:
        raise BenchRepoError(f"refusing refspec {refspec!r}: only <40-hex sha>:refs/heads/main is ever pushed")


async def push_with_git(url: str, refspec: str, checkout: Path, token: str) -> None:
    """Push through `run_git`: the token goes in an environment variable, never argv.

    No `--force`: a repository that already holds a different `main` is refused,
    not overwritten. Exactly one refspec, re-checked here (`check_push_target`).
    """
    check_push_target(url, refspec)
    host = urlsplit(url).hostname or DEFAULT_CREDENTIAL_HOST
    await run_git(
        "push", url, refspec, cwd=checkout, token=token, credential_host=host, timeout=CLONE_TIMEOUT_SECONDS
    )


async def _has_commit(checkout: Path, sha: str) -> bool:
    try:
        await run_git("cat-file", "-e", f"{sha}^{{commit}}", cwd=checkout)
    except GitError:
        return False
    return True


@contextlib.asynccontextmanager
async def upstream_checkout(spec: InstanceSpec, cache_dir: Path) -> AsyncIterator[Path]:
    """A full, non-shallow clone of the instance's upstream repository holding its base commit.

    The cache that `select` leaves under `eval/cache/<owner>__<name>` when it is
    usable; otherwise a fresh full clone in a temporary directory. A shallow
    cache is not used: the push would then carry a truncated history, and the
    pipeline's clone of the benchmark repository must be full for incremental
    indexing to work.
    """
    if _UPSTREAM.fullmatch(spec.repo) is None:
        raise BenchRepoError(f"{spec.instance_id}: upstream repo {spec.repo!r} is not owner/name")
    owner, name = spec.repo.split("/")
    try:
        cached = resolve_within(cache_dir, f"{owner}__{name}") if cache_dir.is_dir() else None
    except PathEscapesRoot as exc:
        raise BenchRepoError(f"{spec.instance_id}: {exc}") from None
    if cached is not None and cached.is_dir():
        if not await GitRepo(cached).is_shallow() and await _has_commit(cached, spec.base_commit):
            yield cached
            return

    with tempfile.TemporaryDirectory(prefix="repolace-bench-") as scratch:
        destination = Path(scratch) / "clone"
        # `clone()` wants a branch name this harness does not know. A plain
        # `git clone` fetches every ref and is full unless the environment forces
        # otherwise, which the check below catches.
        # `--no-checkout`: only history is needed for the push, and checking out an
        # upstream tree on the host would write its files (symlinks included) for nothing.
        await run_git(
            "clone", "--no-checkout", f"{GITHUB_GIT}/{spec.repo}.git", str(destination), timeout=CLONE_TIMEOUT_SECONDS
        )
        if await GitRepo(destination).is_shallow():
            raise BenchRepoError(f"{spec.instance_id}: clone of {spec.repo} is shallow")
        if not await _has_commit(destination, spec.base_commit):
            raise BenchRepoError(f"{spec.instance_id}: {spec.repo} has no commit {spec.base_commit}")
        yield destination


# --- operations -----------------------------------------------------------------


@dataclass
class ForkReport:
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    #: instance_id -> why. A repository this tool created is still listed in the
    #: mapping file, as incomplete, so `--delete` can remove it.
    not_ready: dict[str, str] = field(default_factory=dict)
    ready: list[str] = field(default_factory=list)


async def create_repos(
    instances: Mapping[str, InstanceSpec],
    github: GithubBench,
    *,
    mapping_path: Path,
    cache_dir: Path,
    git_push: GitPush,
    token: str,
    checkout: Checkout = upstream_checkout,
    git_base_url: str = GITHUB_GIT,
    installation_id: int | None = None,
    out: Callable[[str], None] = print,
) -> ForkReport:
    """Create, lock down and populate one repository per instance, recording each that is ready.

    The order is create, disable Actions, push, then (optionally) the
    installation: Actions go off *before* the first push, because pushing a
    tree that carries workflows would otherwise trigger them. The mapping file is
    rewritten after every repository so a crash keeps what already succeeded.

    A repository that already exists is adopted only if it carries this tool's
    marker (`BENCH_MARKER`) and is private; any other is refused as "not created
    by this tool" and left untouched. Once a repository is known to be this
    tool's, a later failure records it as `incomplete` rather than dropping it, so
    it can still be listed and deleted.
    """
    entries = load_bench_entries(mapping_path)
    report = ForkReport()
    for instance_id, spec in instances.items():
        repo = BenchRepo.for_instance(instance_id)
        ours = False
        try:
            _info, created = await github.create_repo(repo)
            ours = True
            (report.created if created else report.existing).append(instance_id)
            await github.disable_actions(repo)
            async with checkout(spec, cache_dir) as source:
                await git_push(
                    f"{git_base_url}/{repo.full_name}.git", f"{spec.base_commit}:refs/heads/main", source, token
                )
            if installation_id is not None:
                await github.add_to_installation(installation_id, repo)
        except (BenchRepoError, GitError) as exc:
            reason = github.scrub(str(exc))
            report.not_ready[instance_id] = reason
            if ours:
                entries[instance_id] = BenchEntry(repo.full_name, "incomplete")
                _write_bench_repos(mapping_path, entries)
            elif isinstance(exc, NotCreatedByThisTool) and entries.pop(instance_id, None) is not None:
                # We no longer vouch for a repository somebody else now owns.
                _write_bench_repos(mapping_path, entries)
            out(f"NOT READY {repo.full_name}: {reason}")
            continue
        entries[instance_id] = BenchEntry(repo.full_name, "ready")
        _write_bench_repos(mapping_path, entries)
        report.ready.append(instance_id)
        out(f"ready {repo.full_name} ({'created' if created else 'already existed'})")
    return report


async def delete_repos(
    instance_ids: Sequence[str], github: GithubBench, *, mapping_path: Path, out: Callable[[str], None] = print
) -> int:
    """Delete the listed repositories and drop them from the mapping file. Returns how many failed.

    Each repository is looked up and checked for the marker immediately before its
    DELETE (`GithubBench.delete_repo`); one that is not this tool's is a failure and
    stays listed. Repeated ids are deleted once.
    """
    entries = load_bench_entries(mapping_path)
    failures = 0
    for instance_id in dict.fromkeys(instance_ids):
        # Resolved through the validated mapping, never from the argument: an id
        # the file does not list is not touched.
        repo = BenchRepo.from_full_name(entries[instance_id].repo)
        try:
            deleted = await github.delete_repo(repo)
        except BenchRepoError as exc:
            failures += 1
            out(f"FAILED to delete {repo.full_name}: {github.scrub(str(exc))}")
            continue
        del entries[instance_id]
        _write_bench_repos(mapping_path, entries)
        out(f"{'deleted' if deleted else 'already gone'} {repo.full_name}")
    return failures


# --- command line ---------------------------------------------------------------


def _read_token() -> str | None:
    """The only place the token's environment variable is read."""
    return os.environ.get(TOKEN_ENV_VAR)


def _selected(requested: str, available: Sequence[str], what: str) -> list[str]:
    if requested == "all":
        return list(available)
    # Deduplicated, in order: a repeated id must not be acted on twice.
    wanted = list(dict.fromkeys(item for item in requested.split(",") if item))
    unknown = sorted(set(wanted) - set(available))
    if unknown:
        raise BenchRepoError(f"unknown {what}: {', '.join(unknown)}")
    return wanted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repolace-eval fork",
        # `fork --de --y` must not mean `--delete --yes`: a prefix match on a destructive flag.
        allow_abbrev=False,
        description=f"Create the private bench-<instance_id> repositories in {BENCH_OWNER}/ and push each base commit. "
        f"Needs {TOKEN_ENV_VAR}; see the module docstring for scopes.",
    )
    parser.add_argument("--instances", default="all", help="comma-separated instance ids, or 'all'")
    parser.add_argument("--instances-dir", type=Path, default=DEFAULT_INSTANCES_DIR)
    parser.add_argument("--bench-repos-file", type=Path, default=None, help=f"default: <instances-dir>/{MAPPING_FILENAME}")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--add-to-installation", type=int, default=None, metavar="INSTALLATION_ID",
        help="also add each repository to this GitHub App installation (off by default)",
    )
    parser.add_argument("--delete", action="store_true", help="delete the listed bench-* repositories (irreversible)")
    parser.add_argument("--yes", action="store_true", help="with --delete: actually delete instead of listing")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    token_reader: Callable[[], str | None] | None = None,
    git_push: GitPush | None = None,
    checkout: Checkout | None = None,
    api_url: str = GITHUB_API,
    git_url: str = GITHUB_GIT,
) -> int:
    """`repolace-eval fork`. The keyword arguments are test seams, not flags."""
    args = build_parser().parse_args(argv)

    mapping_path = args.bench_repos_file or args.instances_dir / MAPPING_FILENAME
    try:
        if args.add_to_installation is not None and args.add_to_installation <= 0:
            raise BenchRepoError("--add-to-installation must be a positive installation id")
        if args.delete:
            return _delete_main(args, mapping_path, transport, token_reader, api_url)
        instances = load_instances(args.instances_dir)
        chosen = _selected(args.instances, list(instances), "instance(s)")
    except (BenchRepoError, InstanceError) as exc:
        print(f"repolace-eval fork: {exc}", file=sys.stderr)
        return 2

    token = ((token_reader or _read_token)() or "").strip()
    if not token:
        print(f"repolace-eval fork: set {TOKEN_ENV_VAR} (see the scopes in bench_repos.py)", file=sys.stderr)
        return 2

    async def run() -> ForkReport:
        github = GithubBench(token, base_url=api_url, transport=transport)
        try:
            return await create_repos(
                {instance_id: instances[instance_id] for instance_id in chosen},
                github,
                mapping_path=mapping_path,
                cache_dir=args.cache_dir,
                git_push=git_push or push_with_git,
                token=token,
                checkout=checkout or upstream_checkout,
                git_base_url=git_url,
                installation_id=args.add_to_installation,
            )
        finally:
            await github.aclose()

    try:
        report = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 -- the last line of defence for the token's hygiene
        print(f"repolace-eval fork: {_scrub(f'{type(exc).__name__}: {exc}', token)}", file=sys.stderr)
        return 1
    print(
        f"{len(report.ready)} ready ({len(report.created)} created, {len(report.existing)} already existed), "
        f"{len(report.not_ready)} not ready"
    )
    if report.not_ready:
        print(
            "Not-ready repositories were left on GitHub and are not in the mapping file; "
            "re-run to retry them, or delete them by hand.",
            file=sys.stderr,
        )
    return 1 if report.not_ready else 0


def _delete_main(
    args: argparse.Namespace,
    mapping_path: Path,
    transport: httpx.AsyncBaseTransport | None,
    token_reader: Callable[[], str | None] | None,
    api_url: str,
) -> int:
    entries = load_bench_entries(mapping_path)
    chosen = _selected(args.instances, sorted(entries), f"listed instance(s) in {mapping_path}")
    if not chosen:
        print("nothing listed to delete")
        return 0
    for instance_id in chosen:
        entry = entries[instance_id]
        note = "" if entry.status == "ready" else f" ({entry.status})"
        print(f"{'would delete' if not args.yes else 'deleting'} {entry.repo}{note}")
    if not args.yes:
        print("repolace-eval fork: deletion is irreversible; re-run with --yes to do it", file=sys.stderr)
        return 2
    token = ((token_reader or _read_token)() or "").strip()
    if not token:
        print(f"repolace-eval fork: set {TOKEN_ENV_VAR}", file=sys.stderr)
        return 2

    async def run() -> int:
        github = GithubBench(token, base_url=api_url, transport=transport)
        try:
            return await delete_repos(chosen, github, mapping_path=mapping_path)
        finally:
            await github.aclose()

    try:
        failures = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        print(f"repolace-eval fork: {_scrub(f'{type(exc).__name__}: {exc}', token)}", file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
