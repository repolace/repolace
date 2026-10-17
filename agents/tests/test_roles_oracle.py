"""The no-oracle protocol under the roles: nothing the hidden tests did reaches any role, or steers one.

The same two halves as `test_graph_oracle.py`, run through the roles graph. A sentinel planted in
every field of the baseline and of every attempt result must appear in no request any role
receives (with the product-mode control that proves the search can find it). And worlds that
differ only in the hidden tests must give the same stage sequence, attempt count, stop reason,
steps and messages: routing to the debugger or the reviewer is a function of the filtered
feedback, never of the hidden outcomes, so the one bit the single agent leaks (did the scored
run complete) is all a roles run leaks too.
"""

import pytest
from agents_support import ScriptedLLM, ScriptedVerifier, attempt_record, reply, suite, tool_call
from repolace_agents.contracts import AgentLimits, IssueContext, StopReason
from roles_support import A, B, FakeGit, plan_reply, review_reply, run_roles, stages
from test_graph_oracle import ALL_FIELDS, BENCHMARK, HID, HIDDEN_FILE, PRODUCT, SENT, plant

pytestmark = pytest.mark.anyio

LIMITS = AgentLimits(max_attempts=3, max_steps_per_attempt=16, roles=True)


def submit_with(n: int):
    return reply(None, tool_call("submit", {"summary": f"s{n}"}, id=f"call_s{n}"))


async def run_red_path(*, baseline, attempts, issue, hidden):
    """plan, edit, debug, debug: three red attempts, every role but the reviewer."""
    llm = ScriptedLLM([plan_reply(id="call_p"), submit_with(1), submit_with(2), submit_with(3)])
    verifier = ScriptedVerifier([attempt_record(i, r) for i, r in enumerate(attempts, 1)])
    result = await run_roles(llm, verifier, baseline=baseline, issue=issue, hidden_paths=hidden, limits=LIMITS)
    return result, llm


async def run_clean_path(*, baseline, attempt, issue, hidden):
    """plan, edit, review: one clean attempt, the reviewer included."""
    llm = ScriptedLLM([plan_reply(id="call_p"), submit_with(1), review_reply(id="call_r")])
    verifier = ScriptedVerifier([attempt_record(1, attempt)])
    result = await run_roles(llm, verifier, baseline=baseline, issue=issue, hidden_paths=hidden, limits=LIMITS)
    return result, llm


class TestSentinelNeverReachesAnyRole:
    @pytest.mark.parametrize("field", ALL_FIELDS)
    @pytest.mark.parametrize("where", ["baseline", "attempt", "both"])
    async def test_on_the_red_path_through_the_debugger(self, field, where):
        clean_base, red_attempt = suite(passed=[A, B]), suite(passed=[A])
        baseline = plant(clean_base, field) if where in ("baseline", "both") else clean_base
        attempts = [plant(red_attempt, field) if where in ("attempt", "both") else red_attempt for _ in range(3)]

        result, llm = await run_red_path(baseline=baseline, attempts=attempts, issue=BENCHMARK, hidden=frozenset({HIDDEN_FILE}))

        assert result.stop_reason is StopReason.MAX_ATTEMPTS and stages(llm) == ["plan", "agent", "agent", "agent"]
        for index, call in enumerate(llm.calls):
            assert SENT not in call.text(), f"call {index + 1} ({call.stage}): the sentinel in {where}.{field} reached the model"

    @pytest.mark.parametrize("field", [field for field in ALL_FIELDS if field != "error"])
    async def test_on_the_clean_path_through_the_reviewer(self, field):
        """Planted in both runs, so the attempt stays clean and the reviewer runs (an `error`
        would make it unscoreable, which is the red path above)."""
        # A hidden id that went green is the clean path's oracle: it must not reach the reviewer.
        baseline = plant(suite(passed=[A, B], failed=[HID]), field)
        attempt = plant(suite(passed=[A, B, HID]), field)

        result, llm = await run_clean_path(baseline=baseline, attempt=attempt, issue=BENCHMARK, hidden=frozenset({HIDDEN_FILE}))

        assert stages(llm) == ["plan", "agent", "review"] and result.attempts == 1
        assert all(SENT not in call.text() for call in llm.calls)

    @pytest.mark.parametrize("field", ["failed", "stdout_tail", "collect_failures"])
    async def test_control_the_same_sentinel_is_found_where_it_is_allowed_to_be(self, field):
        """Product mode: the ids are ordinary visible tests, so the debugger is shown them."""
        _, llm = await run_red_path(
            baseline=plant(suite(passed=[A, B]), field), attempts=[plant(suite(passed=[A]), field)] * 3,
            issue=PRODUCT, hidden=frozenset(),
        )

        assert any(SENT in call.text() for call in llm.calls if call.stage == "agent")

    async def test_the_instance_id_and_the_issue_number_and_url_reach_no_role(self):
        issue = IssueContext(424242, "Crash", "body", "https://example.test/upstream/424242", "django__django-424242")

        _, llm = await run_red_path(
            baseline=suite(passed=[A, B]), attempts=[suite(passed=[A])] * 3, issue=issue, hidden=frozenset({HIDDEN_FILE})
        )

        assert all("424242" not in call.text() and "django__django" not in call.text() for call in llm.calls)

    async def test_the_reviewers_diff_comes_from_the_checkout_which_never_holds_the_overlay(self):
        """The reviewer's one input the graph does not build itself; pinned so it stays git's diff, not a result."""
        git = FakeGit(diff="+fixed")
        llm = ScriptedLLM([plan_reply(), submit_with(1), review_reply()])

        await run_roles(llm, ScriptedVerifier([attempt_record(1, plant(suite(passed=[A, B]), "stdout_tail"))]),
                        git=git, issue=BENCHMARK, hidden_paths=frozenset({HIDDEN_FILE}), limits=LIMITS)

        review = llm.calls[2].messages[1]["content"]
        assert "+fixed" in review and SENT not in review


class TestHiddenDoesNotSteerTheRoles:
    H = "tests/test_hidden.py"
    H_ID = f"{H}::t"
    HANG = "verify: suite exceeded its 600s deadline and was killed"
    OOM = "verify: container killed (exit 137); most likely the memory limit"
    IMPORT = "verify: pytest exited 2 (INTERRUPTED)"

    async def outcome(self, baseline, attempt, script):
        llm = ScriptedLLM(script)
        verifier = ScriptedVerifier([attempt_record(i, attempt) for i in range(1, 4)])
        result = await run_roles(
            llm, verifier, baseline=baseline, issue=BENCHMARK, hidden_paths=frozenset({self.H}), limits=LIMITS,
            repo_overview="src/",
        )
        return result.attempts, result.stop_reason, result.steps, stages(llm), [c.messages for c in llm.calls], llm.unused

    def clean_script(self):
        return [plan_reply(id="call_p"), submit_with(1), review_reply(id="call_r")]

    def red_script(self):
        return [plan_reply(id="call_p"), submit_with(1), submit_with(2), submit_with(3)]

    @pytest.fixture
    def completed_baseline(self):
        return suite(passed=[A, B], failed=[self.H_ID])

    async def test_a_hidden_test_red_or_green_sends_the_run_down_the_same_roles(self, completed_baseline):
        red = await self.outcome(completed_baseline, suite(passed=[A, B], failed=[self.H_ID]), self.clean_script())
        green = await self.outcome(completed_baseline, suite(passed=[A, B, self.H_ID]), self.clean_script())

        assert red == green
        assert red[:4] == (1, StopReason.SUBMITTED, 3, ["plan", "agent", "review"])

    async def test_a_hidden_hang_oom_and_import_error_are_indistinguishable_to_the_roles(self, completed_baseline):
        hang = await self.outcome(completed_baseline, suite(passed=[A, B], error=self.HANG), self.red_script())
        oom = await self.outcome(completed_baseline, suite(passed=[A], error=self.OOM), self.red_script())
        broken = await self.outcome(completed_baseline, suite(error=self.IMPORT, collect_failures=[self.H]), self.red_script())

        assert hang == oom == broken
        assert hang[:4] == (3, StopReason.MAX_ATTEMPTS, 4, ["plan", "agent", "agent", "agent"])

    async def test_the_one_remaining_bit_is_visible_so_the_comparison_above_is_not_vacuous(self, completed_baseline):
        completed = await self.outcome(completed_baseline, suite(passed=[A, B]), self.clean_script())
        errored = await self.outcome(completed_baseline, suite(passed=[A, B], error=self.HANG), self.red_script())

        assert completed[3] != errored[3]

    @pytest.mark.parametrize("hidden_tests", [1, 7, 40])
    async def test_the_number_of_hidden_tests_never_changes_what_any_role_sees(self, hidden_tests):
        def world(n):
            ids = [f"{self.H}::t{i}" for i in range(n)]
            return suite(passed=[A, B, *ids[::2]], failed=ids[1::2]), suite(passed=[A, *ids[::2]], failed=ids[1::2])

        reference = await self.outcome(*world(0), self.red_script())
        varied = await self.outcome(*world(hidden_tests), self.red_script())

        assert varied == reference and reference[1] is StopReason.MAX_ATTEMPTS
