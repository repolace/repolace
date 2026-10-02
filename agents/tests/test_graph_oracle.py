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

from agents_support import (
    ScriptedLLM,
    ScriptedVerifier,
    attempt_record,
    make_deps,
    reply,
    scripted_toolbox,
    submit_reply,
    suite,
    tool_call,
)

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

#: **Every module that builds or sends model-visible text, except `feedback.py` (the one
#: sanctioned reader).** Written as a constant so extending it is a one-line change.
#: It lists this package's own top-level modules and every module under `tools/` except
#: `tools/sandbox.py`. That one renders a probe `SuiteResult` into a tool result on
#: purpose, so it legitimately reads those fields: it is safe only because a probe never
#: includes the hidden overlay (and the pipeline must filter probe results through
#: `hidden_paths` regardless). It is excluded from the field-read check below, still
#: covered by a narrower whole-object dump check, and pinned by `TestTheProbeRendererIsTheOnlyToolThatReadsAResult`.
#: Add paths here, not a second list.
TOOLS = PACKAGE / "tools"
PROBE_RENDERER = TOOLS / "sandbox.py"
ORACLE_SCANNED_FILES = tuple(PACKAGE / f"{name}.py" for name in ("graph", "prompts", "render", "state", "run")) + tuple(
    path for path in sorted(TOOLS.glob("*.py")) if path != PROBE_RENDERER
)
#: The scanned files in which not even the filter calls may touch `.baseline` / `.result`.
ORACLE_BLIND_FILES = tuple(PACKAGE / f"{name}.py" for name in ("prompts", "render", "state", "run"))

#: Names and attributes that hold a `SuiteResult` or an `AttemptRecord` anywhere in this
#: package, for the dump check below.
ORACLE_NAMES = frozenset({"baseline", "result", "record", "rec", "last_attempt", "attempt_record"})
#: Functions that turn a whole object into text. `repr(record)` would print every field
#: of a `SuiteResult` -- including the hidden node ids -- without ever naming one.
DUMPERS = frozenset({"repr", "str", "asdict", "astuple", "vars", "dumps", "format", "pformat", "ascii"})


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _called_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    return None


def _mentions_oracle(node: ast.AST) -> bool:
    return any(
        (isinstance(n, ast.Name) and n.id in ORACLE_NAMES) or (isinstance(n, ast.Attribute) and n.attr in ORACLE_NAMES)
        for n in ast.walk(node)
    )


def dump_offenders(tree: ast.AST) -> list[str]:
    """Places that stringify a whole oracle-holding object: `repr(x)`, `asdict(x)`, `f"{x}"`, `"%s" % x`."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _called_name(node) in DUMPERS:
            if any(_mentions_oracle(arg) for arg in (*node.args, *(k.value for k in node.keywords))):
                found.append(f"line {node.lineno}: {_called_name(node)}(...)")
        elif isinstance(node, ast.FormattedValue) and _mentions_oracle(node.value):
            found.append(f"line {node.lineno}: f-string interpolation")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and _mentions_oracle(node.right):
            found.append(f"line {node.lineno}: %-formatting")
    return found


class TestOnlyTheFilterReadsTheOracle:
    """A tripwire, not a proof: it fails when someone adds a second reader, and says where."""

    @pytest.mark.parametrize("path", ORACLE_SCANNED_FILES, ids=lambda p: p.name)
    def test_no_module_but_the_filter_touches_a_suite_results_fields(self, path):
        tree = ast.parse(path.read_text())

        touched = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in SUITE_RESULT_FIELDS
        }

        assert touched == set(), f"{path.name} reads SuiteResult field(s) {sorted(touched)}; only feedback.py may"

    @pytest.mark.parametrize("path", ORACLE_BLIND_FILES, ids=lambda p: p.name)
    def test_the_prompt_builders_never_touch_the_baseline_or_an_attempts_result_at_all(self, path):
        tree = ast.parse(path.read_text())

        touched = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ORACLE_ATTRIBUTES}

        assert touched == set(), f"{path.name} reads {sorted(touched)}"

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

    @pytest.mark.parametrize("path", ORACLE_SCANNED_FILES, ids=lambda p: p.name)
    def test_nothing_stringifies_a_whole_result_or_record(self, path):
        """Attribute-name matching misses `repr(record)`, `asdict(rec.result)` and `f"{baseline}"`."""
        assert dump_offenders(ast.parse(path.read_text())) == []

    @pytest.mark.parametrize(
        "leak",
        [
            "repr(record)",
            "str(rec.result)",
            "asdict(deps.baseline)",
            "dataclasses.asdict(last_attempt)",
            "json.dumps(vars(record))",
            "f'{record}'",
            "f'baseline: {deps.baseline!r}'",
            "'%s' % (baseline,)",
            "format(result)",
        ],
    )
    def test_the_dump_check_would_catch_each_of_these(self, leak):
        """The tripwire is only worth keeping if it trips."""
        assert dump_offenders(ast.parse(f"def f(deps, record, rec, baseline, result, last_attempt):\n    return {leak}"))

    @pytest.mark.parametrize("fine", ["repr(deps.limits)", "str(call.id)", "f'{nonce}'", "format(1)", "dict(response.message)"])
    def test_the_dump_check_leaves_ordinary_code_alone(self, fine):
        assert not dump_offenders(
            ast.parse(f"def f(deps, call, nonce, response):\n    return {fine}")
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

    def test_the_scanned_files_all_exist_and_the_list_is_not_empty(self):
        """A renamed module would otherwise fall out of the scan and everything would still pass."""
        assert ORACLE_SCANNED_FILES and all(path.is_file() for path in ORACLE_SCANNED_FILES)

    def test_the_prompt_modules_do_not_import_suite_result(self):
        """By import, not by text: a docstring may name it, but nothing may bring it into scope."""
        for path in ORACLE_BLIND_FILES:
            if path.name == "run.py":
                continue
            tree = ast.parse(path.read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imported.add(node.module or "")
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)

            assert "SuiteResult" not in imported and not any(name.startswith("verify") for name in imported), path.name


class TestHiddenDoesNotSteerTheRun:
    """The audit's E8, on the real graph: worlds that differ only in the hidden tests.

    Same scripted model, same visible results; only what the hidden tests did varies.
    Retry decision, stop reason, spend and every message the model receives must be a
    function of nothing but whether the scored run completed.
    """

    H = "tests/test_hidden.py"
    H_ID = f"{H}::t"
    HANG = "verify: suite exceeded its 600s deadline and was killed"
    OOM = "verify: container killed (exit 137); most likely the memory limit"
    IMPORT = "verify: pytest exited 2 (INTERRUPTED)"

    async def outcome(self, baseline, attempt):
        # Fixed call ids: `tool_call` numbers them from a global counter, so two otherwise
        # identical runs would differ in the transcript for a reason that is not the graph's.
        llm = ScriptedLLM(
            [reply(None, tool_call("submit", {"summary": f"s{i}"}, id=f"call_s{i}")) for i in range(1, 4)]
        )
        verifier = ScriptedVerifier([attempt_record(i, attempt) for i in range(1, 4)])
        toolbox, _ = scripted_toolbox()
        deps = make_deps(
            llm=llm, tools=toolbox, verify_attempt=verifier, baseline=baseline, issue=BENCHMARK,
            hidden_paths=frozenset({self.H}), limits=AgentLimits(max_attempts=3, max_steps_per_attempt=4),
            retrieved=(), repo_overview="src/",
        )
        result = await run_graph(deps, nonce=NONCE)
        return result.attempts, result.stop_reason, result.steps, [c.messages for c in llm.calls]

    @pytest.fixture
    def completed_baseline(self):
        return suite(passed=[A, B], failed=[self.H_ID])

    async def test_a_hidden_test_red_or_green_makes_no_difference(self, completed_baseline):
        red = await self.outcome(completed_baseline, suite(passed=[A, B], failed=[self.H_ID]))
        green = await self.outcome(completed_baseline, suite(passed=[A, B, self.H_ID]))

        assert red == green
        assert red[:3] == (1, StopReason.SUBMITTED, 1)

    async def test_a_hidden_hang_a_hidden_oom_and_a_hidden_import_error_are_indistinguishable(self, completed_baseline):
        hang = await self.outcome(completed_baseline, suite(passed=[A, B], error=self.HANG))
        oom = await self.outcome(completed_baseline, suite(passed=[A], error=self.OOM))
        broken_import = await self.outcome(
            completed_baseline, suite(error=self.IMPORT, collect_failures=[self.H])  # visible results lost too
        )

        assert hang == oom == broken_import
        assert hang[:3] == (3, StopReason.MAX_ATTEMPTS, 3)

    async def test_the_one_remaining_bit_is_visible_so_the_comparison_above_is_not_vacuous(self, completed_baseline):
        completed = await self.outcome(completed_baseline, suite(passed=[A, B]))
        errored = await self.outcome(completed_baseline, suite(passed=[A, B], error=self.HANG))

        assert completed != errored
        assert errored[1] is StopReason.MAX_ATTEMPTS and completed[1] is StopReason.SUBMITTED

    async def test_an_unusable_baseline_looks_the_same_whatever_made_it_unusable(self):
        hang = await self.outcome(suite(passed=[A, B], error=self.HANG), suite(passed=[A, B]))
        oom = await self.outcome(suite(error=self.OOM, collect_failures=[self.H]), suite(passed=[A, B]))

        assert hang == oom

    @pytest.mark.parametrize("hidden_tests", [1, 7, 40])
    async def test_the_number_of_hidden_tests_never_changes_what_the_model_sees(self, hidden_tests):
        """Kills `len(baseline.failed)` or `len(result.failed)` appended to any message: those
        counts include the hidden tests, so the transcript would differ with their number. The
        static tripwire catches the attribute names; this catches the behaviour."""
        def world(n):
            ids = [f"{self.H}::t{i}" for i in range(n)]
            baseline = suite(passed=[A, B, *ids[::2]], failed=ids[1::2])
            attempt = suite(passed=[A, *ids[::2]], failed=ids[1::2])  # B regresses: three retries
            return baseline, attempt

        reference = await self.outcome(*world(0))
        varied = await self.outcome(*world(hidden_tests))

        assert varied == reference
        assert reference[1] is StopReason.MAX_ATTEMPTS and len(reference[3]) == 3


class TestTheProbeRendererIsTheOnlyToolThatReadsAResult:
    """`tools/sandbox.py` prints a probe's `SuiteResult`; that is safe only while probes never carry the overlay.

    The field-read tripwire above skips this one file because reading those fields is its job.
    These tests make the exception narrow and visible: it stays the only tool that reads a
    result, the dump check still covers it, and nothing under `tools/` can even name the
    hidden-test machinery, so a probe cannot be handed the overlay by a later edit to a tool.
    """

    HIDDEN_NAMES = frozenset({"overlay", "hidden_paths", "hidden"})

    def test_the_exclusion_is_not_stale(self):
        """If the renderer stopped reading result fields, the exception could be dropped."""
        tree = ast.parse(PROBE_RENDERER.read_text())

        assert any(isinstance(n, ast.Attribute) and n.attr in SUITE_RESULT_FIELDS for n in ast.walk(tree))

    def test_no_other_tool_module_is_excluded_from_the_scan(self):
        tool_files = set(TOOLS.glob("*.py"))

        assert tool_files - set(ORACLE_SCANNED_FILES) == {PROBE_RENDERER}

    @staticmethod
    def bare_object_dumps(tree: ast.AST) -> list[str]:
        """Stringifications of a whole result object (`repr(result)`, `f"{result}"`), not of its fields.

        Narrower than `dump_offenders` on purpose: the renderer formats `len(result.failed)` and
        `result.exit_code` all day, which that check (any mention of an oracle name) would flag.
        What must never happen here is dumping the object, which prints every field at once.
        """

        def bare(node: ast.AST) -> bool:
            return isinstance(node, ast.Name) and node.id in ORACLE_NAMES

        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) in DUMPERS:
                if any(bare(arg) for arg in (*node.args, *(k.value for k in node.keywords))):
                    found.append(f"line {node.lineno}: {_called_name(node)}(<whole object>)")
            elif isinstance(node, ast.FormattedValue) and bare(node.value):
                found.append(f"line {node.lineno}: f-string interpolation of a whole object")
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
                right = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
                if any(bare(item) for item in right):
                    found.append(f"line {node.lineno}: %-formatting of a whole object")
        return found

    def test_the_probe_renderer_never_stringifies_a_whole_result(self):
        assert self.bare_object_dumps(ast.parse(PROBE_RENDERER.read_text())) == []

    @pytest.mark.parametrize(
        "leak", ["repr(result)", "str(result)", "asdict(result)", "f'{result}'", "f'{result!r}'", "'%s' % (result,)"]
    )
    def test_the_narrow_check_would_catch_each_of_these(self, leak):
        assert self.bare_object_dumps(ast.parse(f"def f(result):\n    return {leak}"))

    @pytest.mark.parametrize("fine", ["f'{len(result.failed)} failed'", "f'{result.exit_code}'", "str(result.exit_code)"])
    def test_the_narrow_check_allows_field_counts(self, fine):
        assert self.bare_object_dumps(ast.parse(f"def f(result):\n    return {fine}")) == []

    @pytest.mark.parametrize("path", sorted(TOOLS.glob("*.py")), ids=lambda p: p.name)
    def test_no_tool_module_names_the_overlay_or_hidden_paths(self, path):
        """By AST, so a docstring may explain the rule but no code can reach for the overlay."""
        tree = ast.parse(path.read_text())

        named = {
            name
            for node in ast.walk(tree)
            for name in (
                [node.id] if isinstance(node, ast.Name) else
                [node.attr] if isinstance(node, ast.Attribute) else
                [node.arg] if isinstance(node, ast.arg) else
                [node.arg] if isinstance(node, ast.keyword) and node.arg else
                []
            )
            if name.lower() in self.HIDDEN_NAMES
        }

        assert named == set(), f"{path.name} names {sorted(named)}: a probe must never see the hidden overlay"
