"""The two git calls the roles make, against a real checkout: the reviewer's diff and the debugger's rewind.

`run_graph` builds `GitRepo(path=deps.checkout)` itself when no fake is passed, which is what
`run_agent` always does, so these run the production path: `head_sha` once before the planner,
`diff_from(base)` for the reviewer (the pipeline's `review_diff`), `reset_hard` for `rewind` (the
pipeline's `rewind_to`). The verifier here commits the tree the way `AttemptScorer` does and
returns the real sha, so a rewind resolves to a commit that exists.
"""

from pathlib import Path

import pytest
from agents_support import ScriptedLLM, reply, submit_reply, suite, tool_call
from repolace_agents.contracts import AgentLimits, AttemptRecord, StopReason
from repolace_agents.graph import run_graph
from roles_support import NONCE, A, B, plan_reply, review_reply, role_toolbox, roles_deps, stages
from tools_support import git, make_checkout

pytestmark = pytest.mark.anyio

CORE = "src/pkg/core.py"
LIMITS = AgentLimits(max_attempts=3, max_steps_per_attempt=16, roles=True)


class CommittingVerifier:
    """`verify_attempt` as the scorer does it: commit the tree, return the sha, `None` when nothing changed."""

    def __init__(self, checkout: Path, outcomes):
        self.checkout, self.outcomes, self.calls, self.shas = checkout, list(outcomes), [], {}
        self.base = git(checkout, "rev-parse", "HEAD").strip()

    async def __call__(self, attempt: int):
        self.calls.append(attempt)
        git(self.checkout, "add", "-A")
        if git(self.checkout, "diff", "--cached", "--name-only").strip():
            git(self.checkout, "commit", "-q", "-m", f"attempt {attempt}")
        sha = git(self.checkout, "rev-parse", "HEAD").strip()
        if sha == self.base or sha in self.shas.values():
            return None
        self.shas[attempt] = sha
        return AttemptRecord(attempt, sha, self.outcomes.pop(0), False)


def writer(checkout: Path):
    """`edit_file` and `create_file` fakes that really write, so the tree changes between attempts."""

    def result_for(name, args):
        if name in ("edit_file", "create_file"):
            target = checkout / args["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args.get("new", args.get("content", "")))
            return "written"
        return f"{name} ok"

    return result_for


def edit(path: str, text: str, *, id: str):
    return tool_call("edit_file", {"path": path, "new": text}, id=id)


async def run(checkout: Path, llm, verifier):
    toolbox, _ = role_toolbox(writer(checkout))
    deps = roles_deps(llm, verifier, tools=toolbox, checkout=checkout, limits=LIMITS)
    return await run_graph(deps, nonce=NONCE)


async def test_the_reviewer_reads_the_real_diff_of_the_scored_commit_against_the_base(tmp_path):
    checkout = make_checkout(tmp_path)
    llm = ScriptedLLM([
        plan_reply(),
        reply(None, edit(CORE, "def add(a, b):\n    return b + a\n", id="e1")),
        submit_reply(),
        review_reply(),
    ])

    result = await run(checkout, llm, CommittingVerifier(checkout, [suite(passed=[A, B])]))

    review = llm.calls[3].messages[1]["content"]
    assert f"diff --git a/{CORE} b/{CORE}" in review
    assert "-    return a + b" in review and "+    return b + a" in review
    assert result.stop_reason is StopReason.SUBMITTED and stages(llm) == ["plan", "agent", "agent", "review"]


async def test_rewind_restores_a_scored_attempt_and_drops_every_later_edit(tmp_path):
    checkout = make_checkout(tmp_path)
    verifier = CommittingVerifier(checkout, [suite(passed=[A]), suite(passed=[A, B])])
    debug = reply(
        None,
        edit(CORE, "def add(a, b):\n    return 0\n", id="e2"),
        tool_call("create_file", {"path": "src/pkg/stray.py", "content": "x = 1\n"}, id="e3"),
        tool_call("rewind", {"attempt": 1}, id="r1"),
    )
    llm = ScriptedLLM([
        plan_reply(),
        reply(None, edit(CORE, "def add(a, b):\n    return a + b + 0\n", id="e1")),
        submit_reply(),
        debug,
        submit_reply(),
    ])

    result = await run(checkout, llm, verifier)

    assert (checkout / CORE).read_text() == "def add(a, b):\n    return a + b + 0\n"
    assert not (checkout / "src/pkg/stray.py").exists()
    assert git(checkout, "rev-parse", "HEAD").strip() == verifier.shas[1]
    # Nothing changed after the rewind, so attempt 2 had nothing to score: the scored attempt stands.
    assert verifier.calls == [1, 2] and result.stop_reason is StopReason.NO_CHANGE
    assert result.last_attempt is not None and result.last_attempt.commit_sha == verifier.shas[1]


async def test_rewind_to_an_unscored_attempt_is_refused_and_the_tree_is_untouched(tmp_path):
    checkout = make_checkout(tmp_path)
    verifier = CommittingVerifier(checkout, [suite(passed=[A]), suite(passed=[A, B])])
    debug = reply(
        None,
        edit(CORE, "def add(a, b):\n    return a + b  # repaired\n", id="e2"),
        tool_call("rewind", {"attempt": 2}, id="r2"),
    )
    llm = ScriptedLLM([
        plan_reply(),
        reply(None, edit(CORE, "def add(a, b):\n    return a + b + 0\n", id="e1")),
        submit_reply(),
        debug,
        submit_reply(),
        review_reply(),
    ])

    result = await run(checkout, llm, verifier)

    answers = {m["tool_call_id"]: m["content"] for m in llm.calls[4].messages if m["role"] == "tool"}
    assert "attempt 2 was not scored" in answers["r2"]
    assert (checkout / CORE).read_text() == "def add(a, b):\n    return a + b  # repaired\n"
    assert result.attempts == 2 and result.stop_reason is StopReason.SUBMITTED


async def test_the_base_is_read_once_before_the_planner_and_never_from_a_later_head(tmp_path):
    """Every attempt moves HEAD; the reviewer must still diff against where the run started."""
    checkout = make_checkout(tmp_path)
    base = git(checkout, "rev-parse", "HEAD").strip()
    llm = ScriptedLLM([
        plan_reply(),
        reply(None, edit(CORE, "one\n", id="e1")),
        submit_reply(),
        review_reply("request_changes", "Keep the function."),
        reply(None, edit(CORE, "def add(a, b):\n    return a + b  # two\n", id="e2")),
        submit_reply(),
        review_reply(),
    ])

    await run(checkout, llm, CommittingVerifier(checkout, [suite(passed=[A, B]), suite(passed=[A, B])]))

    second_review = llm.calls[6].messages[1]["content"]
    assert "+    return a + b  # two" in second_review and "-def sub(a, b):" in second_review
    assert "-one" not in second_review, "a diff from the base, not from attempt 1"
    assert base != git(checkout, "rev-parse", "HEAD").strip()
