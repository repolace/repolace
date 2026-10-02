"""Nothing the hidden tests did may reach a prompt, through any route.

`test_feedback.py` proves the filter filters. This file proves the *rest of the
package* never goes around it. The two routes by which the oracle could reach the
model without passing the filter are:

* some code path other than `feedback.py` interpolating `AgentDeps.baseline` or
  `AttemptRecord.result` into a message (`TestSentinelNeverReachesTheModel`:
  plant a sentinel in every field of both, run the whole multi-attempt loop, and
  search every request the model received);
* a future edit adding such a path (`TestOnlyTheFilterReadsTheOracle`: a static
  check that the prompt-building modules never touch those fields).

The sentinel run has a control. A test that finds nothing proves nothing unless
the same search *does* find the sentinel where it is allowed to be.
"""

import ast
import dataclasses
from pathlib import Path

import pytest

import repolace_agents
from repolace_agents.contracts import AgentLimits, IssueContext, StopReason
from repolace_agents.graph import run_graph
from verify.protocol import SuiteResult

from agents_support import ScriptedLLM, ScriptedVerifier, attempt_record, make_deps, scripted_toolbox, submit_reply, suite

pytestmark = pytest.mark.anyio

SENT = "ORACLEsentinel7731"
HIDDEN_FILE = f"tests/test_{SENT}.py"
HID = f"{HIDDEN_FILE}::test_f2p_{SENT}"
A, B = "tests/test_a.py::test_one", "tests/test_a.py::test_two"
NONCE = "n0nce1234"

#: Where the oracle can sit in a result, and how the sentinel is planted there.
ID_FIELDS = ("passed", "failed", "skipped", "xfailed", "did_not_run", "collect_failures")
PATH_FIELDS = ("collected_files", "conftests")
TEXT_FIELDS = ("stdout_tail", "error", "fingerprint")


def plant(result: SuiteResult, field: str) -> SuiteResult:
    """`result` with the sentinel in `field`, in the shape that field holds."""
    if field in ID_FIELDS:
        return dataclasses.replace(result, **{field: (*getattr(result, field), HID)})
    if field in PATH_FIELDS:
        return dataclasses.replace(result, **{field: (*getattr(result, field), HIDDEN_FILE)})
    if field == "fingerprint":
        return dataclasses.replace(result, fingerprint={**result.fingerprint, "ini": {"testpaths": SENT}})
    if field == "error":
        return dataclasses.replace(result, error=f"verify: report claims 9 failures; {HID} among them")
    return dataclasses.replace(result, **{field: f"FAILED {HID} - assert {SENT}"})


ALL_FIELDS = (*ID_FIELDS, *PATH_FIELDS, *TEXT_FIELDS)


async def run_three_attempts(*, baseline, attempts, issue, hidden, replies=3):
    """A full loop that retries to the end: three attempts, each red on a visible test."""
    llm = ScriptedLLM([submit_reply(f"s{i}") for i in range(1, replies + 1)])
    verifier = ScriptedVerifier([attempt_record(i, r) for i, r in enumerate(attempts, 1)])
    toolbox, _ = scripted_toolbox()
    deps = make_deps(
        llm=llm, tools=toolbox, verify_attempt=verifier, baseline=baseline, issue=issue, hidden_paths=hidden,
        limits=AgentLimits(max_attempts=3, max_steps_per_attempt=4),
        retrieved=(), repo_overview="src/\n  app.py",
    )
    result = await run_graph(deps, nonce=NONCE)
    return result, llm


BENCHMARK = IssueContext(7, "Crash on empty input", "parse() raises IndexError", "https://example.test/7", "inst-1")
PRODUCT = IssueContext(7, "Crash on empty input", "parse() raises IndexError", "https://example.test/7", None)


class TestSentinelNeverReachesTheModel:
    @pytest.mark.parametrize("field", ALL_FIELDS)
    @pytest.mark.parametrize("where", ["baseline", "attempt", "both"])
    async def test_a_sentinel_in_any_field_of_any_result_appears_in_no_request(self, field, where):
        clean_base, red_attempt = suite(passed=[A, B]), suite(passed=[A])
        baseline = plant(clean_base, field) if where in ("baseline", "both") else clean_base
        attempts = [plant(red_attempt, field) if where in ("attempt", "both") else red_attempt for _ in range(3)]

        result, llm = await run_three_attempts(
            baseline=baseline, attempts=attempts, issue=BENCHMARK, hidden=frozenset({HIDDEN_FILE})
        )

        assert result.stop_reason is StopReason.MAX_ATTEMPTS and result.attempts == 3
        assert len(llm.calls) == 3
        for index, call in enumerate(llm.calls):
            assert SENT not in call.text(), f"call {index + 1}: the sentinel in {where}.{field} reached the model"

    async def test_the_retry_framing_was_actually_sent_so_the_search_above_is_not_vacuous(self):
        _, llm = await run_three_attempts(
            baseline=plant(suite(passed=[A, B]), "failed"),
            attempts=[plant(suite(passed=[A]), "failed")] * 3,
            issue=BENCHMARK, hidden=frozenset({HIDDEN_FILE}),
        )

        for call in llm.calls[1:]:
            last = call.messages[-1]["content"]
            assert "Problems found" in last and B in last
        assert "Baseline test run at the base commit" in llm.calls[0].messages[1]["content"]

    @pytest.mark.parametrize("field", ["failed", "stdout_tail", "collect_failures"])
    async def test_control_the_same_sentinel_is_found_where_it_is_allowed_to_be(self, field):
        """Product mode: no hidden paths, no instance id. The ids are ordinary visible tests
        and the stdout is shown, so the same search must find the sentinel -- or it cannot
        be trusted to find it anywhere."""
        _, llm = await run_three_attempts(
            baseline=plant(suite(passed=[A, B]), field),
            attempts=[plant(suite(passed=[A]), field)] * 3,
            issue=PRODUCT, hidden=frozenset(),
        )

        assert any(SENT in call.text() for call in llm.calls)

    async def test_a_benchmark_task_with_an_empty_overlay_still_never_shows_raw_output(self):
        """`instance_id` set, `hidden_paths` empty: the oracle is in play, so stdout and raw errors stay out."""
        attempts = [plant(plant(suite(passed=[A]), "stdout_tail"), "error")] * 3

        _, llm = await run_three_attempts(
            baseline=suite(passed=[A, B]), attempts=attempts, issue=BENCHMARK, hidden=frozenset()
        )

        assert all(SENT not in call.text() for call in llm.calls)

    async def test_an_unusable_baseline_is_a_category_in_the_prompt_never_its_text(self):
        baseline = plant(suite(passed=[A, B]), "error")

        _, llm = await run_three_attempts(
            baseline=baseline, attempts=[suite(passed=[A, B])] * 3, issue=BENCHMARK, hidden=frozenset({HIDDEN_FILE})
        )

        assert "baseline run at the base commit was unusable" in llm.calls[0].messages[1]["content"]
        assert all(SENT not in call.text() and "9 failures" not in call.text() for call in llm.calls)

    async def test_hidden_paths_themselves_are_never_shown(self):
        """Which files hold the hidden tests is itself a hint at what the fix touches."""
        _, llm = await run_three_attempts(
            baseline=suite(passed=[A, B]), attempts=[suite(passed=[A])] * 3, issue=BENCHMARK,
            hidden=frozenset({HIDDEN_FILE, "tests/hidden_dir"}),
        )

        assert all("tests/hidden_dir" not in call.text() and HIDDEN_FILE not in call.text() for call in llm.calls)

    async def test_the_instance_id_and_the_issue_number_and_url_are_never_shown(self):
        issue = IssueContext(424242, "Crash", "body", "https://example.test/upstream/424242", "django__django-424242")

        _, llm = await run_three_attempts(
            baseline=suite(passed=[A, B]), attempts=[suite(passed=[A])] * 3, issue=issue, hidden=frozenset({HIDDEN_FILE})
        )

        assert all("424242" not in call.text() and "django__django" not in call.text() for call in llm.calls)


PACKAGE = Path(repolace_agents.__file__).parent
#: The only functions that may be handed the oracle, and only as an argument.
FILTER_FUNCTIONS = {"visible_feedback", "baseline_summary"}
ORACLE_ATTRIBUTES = {"baseline", "result"}
SUITE_RESULT_FIELDS = {f.name for f in dataclasses.fields(SuiteResult)}


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _called_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    return None


class TestOnlyTheFilterReadsTheOracle:
    """A tripwire, not a proof: it fails when someone adds a second reader, and says where."""

    @pytest.mark.parametrize("module", ["graph", "prompts", "render", "state", "run"])
    def test_no_module_but_the_filter_touches_a_suite_results_fields(self, module):
        tree = ast.parse((PACKAGE / f"{module}.py").read_text())

        touched = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in SUITE_RESULT_FIELDS
        }

        assert touched == set(), f"{module}.py reads SuiteResult field(s) {sorted(touched)}; only feedback.py may"

    @pytest.mark.parametrize("module", ["prompts", "render", "state", "run"])
    def test_the_prompt_builders_never_touch_the_baseline_or_an_attempts_result_at_all(self, module):
        tree = ast.parse((PACKAGE / f"{module}.py").read_text())

        touched = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ORACLE_ATTRIBUTES}

        assert touched == set(), f"{module}.py reads {sorted(touched)}"

    def test_the_graph_hands_the_oracle_only_to_the_filter_as_an_argument(self):
        tree = ast.parse((PACKAGE / "graph.py").read_text())
        parents = _parents(tree)

        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ORACLE_ATTRIBUTES:
                parent = parents[node]
                # A keyword argument's value sits under an `ast.keyword`, which sits under the call.
                call = parents[parent] if isinstance(parent, ast.keyword) else parent
                if _called_name(call) not in FILTER_FUNCTIONS:
                    offenders.append(f"line {node.lineno}: .{node.attr}")

        assert offenders == []
        # And it does hand it over, so the check above is not passing on an empty set.
        assert any(
            isinstance(n, ast.Attribute) and n.attr in ORACLE_ATTRIBUTES for n in ast.walk(tree)
        )

    def test_the_check_would_catch_a_second_reader(self):
        """The tripwire is only worth keeping if it trips."""
        leaky = ast.parse("def f(deps, rec):\n    return f'baseline: {deps.baseline.failed} {rec.result}'")
        parents = _parents(leaky)

        offenders = [
            n.attr for n in ast.walk(leaky)
            if isinstance(n, ast.Attribute) and n.attr in ORACLE_ATTRIBUTES and _called_name(parents[n]) not in FILTER_FUNCTIONS
        ]

        assert sorted(offenders) == ["baseline", "result"]

    def test_the_prompt_modules_do_not_import_suite_result(self):
        """By import, not by text: a docstring may name it, but nothing may bring it into scope."""
        for module in ("prompts", "render", "state"):
            tree = ast.parse((PACKAGE / f"{module}.py").read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imported.add(node.module or "")
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)

            assert "SuiteResult" not in imported and not any(name.startswith("verify") for name in imported), module
