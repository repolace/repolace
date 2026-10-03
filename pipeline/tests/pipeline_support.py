"""Builders for pipeline test data.

Plain constructors rather than fixtures: the stub editor takes plain data by
design, so building it should not need pytest machinery.
"""

import dataclasses
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from repolace_shared.db.models import GithubInstallation, RegisteredRepo, Task, TaskStatus
from repolace_shared.git import task_workspace
from repolace_shared.github.schemas import PullRequest
from verify.protocol import SuiteResult
from verify.scoring import Score, Verdict

from repolace_agents.contracts import AgentDeps, AgentResult, AttemptRecord, StopReason
from repolace_pipeline.context import RetrievedChunk
from repolace_pipeline.edit import StubEditRequest
from repolace_pipeline.pr import PrFacts

TASK_ID = uuid.UUID("2f8a1c4e-0000-4000-8000-000000000001")


def chunk(
    file_path: str = "src/app.py",
    start_line: int = 3,
    end_line: int = 9,
    chunk_type: str = "function",
    symbol_name: str = "parse_config",
    class_name: str | None = None,
    rrf_score: float = 0.0328,
    semantic_rank: int | None = 1,
    keyword_rank: int | None = 3,
) -> RetrievedChunk:
    return RetrievedChunk(
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        chunk_type=chunk_type,
        symbol_name=symbol_name,
        class_name=class_name,
        rrf_score=rrf_score,
        semantic_rank=semantic_rank,
        keyword_rank=keyword_rank,
    )


def request(*chunks: RetrievedChunk, indexed_chunk_count: int = 238) -> StubEditRequest:
    return StubEditRequest(
        task_id=TASK_ID,
        issue_number=7,
        issue_title="parse_config crashes on an empty file",
        issue_url="https://github.com/acme/sample/issues/7",
        target_branch="main",
        base_sha="a1b2c3d4e5f6a7b8c9d0",
        indexed_chunk_count=indexed_chunk_count,
        retrieved=chunks or (chunk(),),
    )


def empty_request() -> StubEditRequest:
    """A request whose retrieval came back empty -- the case the editor must refuse."""
    return dataclasses.replace(request(), retrieved=())


def pr_facts(**overrides) -> PrFacts:
    """A product-mode `PrFacts` for a clean, submitted change. Override what a test is about."""
    fields = dict(
        task_id=TASK_ID,
        issue_number=7,
        issue_title="parse_config crashes on an empty file",
        instance_id=None,
        plumbing_only=False,
        summary="Return an empty dict when the file is empty.",
        changed_files=("src/app.py",),
        baseline=SuiteResult(passed=("t::a", "t::b"), failed=(), skipped=("t::s",)),
        final=SuiteResult(passed=("t::a", "t::b", "t::c"), failed=(), skipped=("t::s",)),
        verdict=Verdict(ok=True, reason="no regression, silenced failure or new collection error found"),
        scored=Score(outcome=None, reason="inadmissible", inadmissible=True),
        expected_fail_to_pass=None,
        attempts=2,
        stop_reason="submitted",
        model="claude-sonnet-5-5",
        cost_usd=Decimal("0.4213"),
        retrieved=(chunk(),),
    )
    return PrFacts(**{**fields, **overrides})


def benchmark_facts(**overrides) -> PrFacts:
    """A benchmark-mode `PrFacts`: `instance_id` is the one switch, the rest follows from it."""
    fields = dict(
        instance_id="psf__requests-2317",
        expected_fail_to_pass=("tests/test_hidden.py::test_f2p",),
        final=SuiteResult(passed=("t::a", "tests/test_hidden.py::test_f2p")),
        baseline=SuiteResult(passed=("t::a",), failed=("tests/test_hidden.py::test_f2p",)),
        scored=Score(
            outcome=None,
            reason="1 fail-to-pass, no regressions",
            fail_to_pass=("tests/test_hidden.py::test_f2p",),
        ),
    )
    return pr_facts(**{**fields, **overrides})


# --- a real git remote -------------------------------------------------------
#
# Re-implemented here rather than imported from `shared/tests/shared_support.py`: pytest's
# prepend import mode makes that module reachable from this suite only by accident of
# `sys.path`, which is the shadowing the `<package>_support.py` convention exists to dodge.

#: A test repo has no committer identity and may inherit a global signing requirement,
#: either of which fails the commit outright.
AUTHOR_ARGS = ("-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *AUTHOR_ARGS, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


APP_SOURCE = '''def parse_config(path):
    """Parse the key=value config file at path into a dict."""
    with open(path) as handle:
        return dict(line.strip().split("=", 1) for line in handle if "=" in line)


def render_report(rows):
    """Render rows as a plain text report."""
    return "\\n".join(str(row) for row in rows)
'''

TEST_SOURCE = '''from src.app import parse_config


def test_parse_config_reads_pairs(tmp_path):
    path = tmp_path / "c.cfg"
    path.write_text("a=1\\n")
    assert parse_config(path) == {"a": "1"}
'''


def make_source_repo(root: Path) -> Path:
    """A small Python repo with one commit on `main`: a module, a test, a README."""
    root.mkdir(parents=True)
    git(root, "init", "--initial-branch=main", ".")
    write(root / "README.md", "# sample\n")
    write(root / "src" / "__init__.py", "")
    write(root / "src" / "app.py", APP_SOURCE)
    write(root / "tests" / "test_app.py", TEST_SOURCE)
    git(root, "add", "-A")
    git(root, "commit", "-m", "first")
    return root


def make_bare_origin(source: Path, destination: Path) -> Path:
    """A bare clone standing in for the GitHub remote, so a push has somewhere real to land."""
    subprocess.run(["git", "clone", "--bare", str(source), str(destination)], check=True, capture_output=True)
    return destination


def local_workspace_factory(url: str):
    """A `workspace_factory` that clones a local remote instead of github.com."""
    return partial(task_workspace, clone_url=url)


# --- the GitHub boundary -----------------------------------------------------


class FakeGithubClient:
    """The three calls the pipeline makes, recording what it was asked.

    Not a mock: it has the real client's signatures, returns the real schema types, and
    refuses nothing by itself. `permissions` is what the installation reports granted,
    so a test can make the preflight fail the way a missing App permission does.
    """

    def __init__(self, permissions: dict[str, str] | None = None) -> None:
        self.permissions = (
            {"contents": "write", "pull_requests": "write"} if permissions is None else permissions
        )
        self.pull_requests: list[dict[str, Any]] = []
        self.token_requests = 0
        self.closed = False

    async def get_installation(self, installation_id: int):
        return SimpleNamespace(id=installation_id, permissions=self.permissions)

    async def get_installation_token(self, installation_id: int, min_ttl_seconds: float = 60.0) -> str:
        self.token_requests += 1
        return "ghs_fake_installation_token"

    async def create_pull_request(
        self, installation_id: int, owner: str, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        number = len(self.pull_requests) + 1
        self.pull_requests.append(
            {
                "installation_id": installation_id, "owner": owner, "repo": repo,
                "head": head, "base": base, "title": title, "body": body, "number": number,
            }
        )
        return PullRequest(number=number, html_url=f"https://github.test/{owner}/{repo}/pull/{number}", state="open")

    async def aclose(self) -> None:
        self.closed = True


# --- database rows -----------------------------------------------------------

INSTALLATION_ID = 4242


async def seed_task(session, *, repo_overrides: dict | None = None, **task_overrides) -> Task:
    """Installation -> repo -> task, committed. Returns the task.

    Re-implemented from `shared/tests/db_support.py` for the reason above.
    """
    session.add(
        GithubInstallation(id=INSTALLATION_ID, account_login="acme", account_id=1, account_type="Organization")
    )
    repo_fields = {
        "id": uuid.uuid4(),
        "installation_id": INSTALLATION_ID,
        "github_repo_id": 99,
        "owner": "acme",
        "name": "sample",
        "full_name": "acme/sample",
        "default_branch": "main",
        "private": False,
    }
    repo = RegisteredRepo(**{**repo_fields, **(repo_overrides or {})})
    session.add(repo)
    await session.flush()
    task_fields = {
        "id": uuid.uuid4(),
        "repo_id": repo.id,
        "issue_number": 7,
        "issue_title": "parse_config crashes on an empty config file",
        "issue_url": "https://github.com/acme/sample/issues/7",
        "target_branch": "main",
        "status": TaskStatus.QUEUED,
    }
    task = Task(**{**task_fields, **task_overrides})
    session.add(task)
    await session.commit()
    return task


# --- an agent made of a script -----------------------------------------------


@dataclass
class ScriptedAgent:
    """An `AgentRunner` whose behaviour is a function the test writes.

    Records every `AgentDeps` it was called with, so a test can assert on what the pipeline
    handed it (hidden paths, the issue, the baseline) as well as on what it did with it.
    """

    script: Callable[[AgentDeps], Awaitable[AgentResult]]
    calls: list[AgentDeps] = field(default_factory=list)

    async def __call__(self, deps: AgentDeps) -> AgentResult:
        self.calls.append(deps)
        return await self.script(deps)


def agent_result(
    stop_reason: StopReason = StopReason.SUBMITTED,
    last_attempt: AttemptRecord | None = None,
    *,
    summary: str | None = "Fixed it.",
    attempts: int | None = None,
    steps: int = 4,
) -> AgentResult:
    return AgentResult(
        stop_reason=stop_reason,
        summary=summary,
        attempts=attempts if attempts is not None else (1 if last_attempt is not None else 0),
        steps=steps,
        last_attempt=last_attempt,
    )


def edit(deps: AgentDeps, relative_path: str, text: str) -> None:
    """What an agent's `edit_file` does to the tree, without the tool: write a file in the checkout."""
    write(deps.checkout / relative_path, text)


@dataclass(frozen=True)
class FakeToolCall:
    """The four attributes `ToolBox.dispatch` reads and nothing from the gateway.

    The real `repolace_gateway.client.ToolCall` has the same four plus `raw_arguments`;
    building this keeps litellm out of the process.
    """

    name: str
    arguments: dict = field(default_factory=dict)
    id: str = "call_1"
    parse_error: str | None = None
