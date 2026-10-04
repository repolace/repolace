"""What the agent's tools may learn from the pipeline: probes, refusals, and the wiring.

The filter tests state the property they defend -- "nothing under a hidden path survives" --
and then try to break it with the spellings a real run produces (an absolute container path
for a conftest, a directory overlay, `./`). The wiring tests use a real clone and the real
`Verifier`; only the sandbox behind it is the shared fake.
"""

import pytest
from repolace_shared.git import task_workspace
from verify.protocol import RepoSpec, ScriptResult, SuiteResult
from verify.stage import Verifier
from verify.testing import FakeBackend

from repolace_agents import feedback
from repolace_agents.tools.base import ToolLimits
from repolace_pipeline.agent_context import (
    build_tool_context,
    protected_check,
    visible_probe,
)

from pipeline_support import TASK_ID

pytestmark = pytest.mark.anyio

HIDDEN = {"tests/test_hidden.py"}
HIDDEN_ID = "tests/test_hidden.py::test_f2p"
VISIBLE_ID = "tests/test_app.py::test_parse_config_reads_pairs"


def suite(**overrides) -> SuiteResult:
    return SuiteResult(**overrides)


class TestVisibleProbe:
    def test_a_hidden_id_is_dropped_from_every_bucket(self):
        raw = suite(
            passed=(VISIBLE_ID, HIDDEN_ID),
            failed=(HIDDEN_ID + "[x]",),
            skipped=(HIDDEN_ID + "[s]",),
            xfailed=(HIDDEN_ID + "[xf]",),
            did_not_run=(HIDDEN_ID + "[d]",),
            collect_failures=("tests/test_hidden.py",),
        )

        clean = visible_probe(raw, HIDDEN)

        assert clean.passed == (VISIBLE_ID,)
        assert clean.failed == () and clean.skipped == () and clean.xfailed == ()
        assert clean.did_not_run == () and clean.collect_failures == ()

    def test_a_file_under_a_hidden_directory_is_dropped(self):
        clean = visible_probe(suite(passed=("hidden_dir/test_a.py::t", VISIBLE_ID)), {"hidden_dir"})

        assert clean.passed == (VISIBLE_ID,)

    @pytest.mark.parametrize(
        "spelling", ["./tests/test_hidden.py::t", "tests//test_hidden.py::t", "/tests/test_hidden.py::t"]
    )
    def test_the_other_spellings_of_a_hidden_path_are_dropped_too(self, spelling):
        assert visible_probe(suite(passed=(spelling,)), HIDDEN).passed == ()

    def test_a_similar_looking_visible_path_is_kept(self):
        near = ("tests/test_hidden_helpers.py::t", "tests/test_hidden.py2::t", "xtests/test_hidden.py::t")

        assert visible_probe(suite(passed=near), HIDDEN).passed == near

    def test_collected_files_and_conftests_under_hidden_paths_are_dropped(self):
        raw = suite(
            collected_files=("tests/test_app.py", "tests/test_hidden.py"),
            conftests=("/repo/conftest.py", "/repo/tests/test_hidden.py"),
        )

        clean = visible_probe(raw, HIDDEN)

        assert clean.collected_files == ("tests/test_app.py",)
        assert clean.conftests == ("/repo/conftest.py",)

    def test_a_hidden_conftest_is_matched_by_its_container_path(self):
        """pytest reports a conftest by absolute path; compared raw it would never match `tests/conftest.py`."""
        raw = suite(conftests=("/repo/tests/conftest.py", "/repo/conftest.py"))

        clean = visible_probe(raw, {"tests/conftest.py"})

        assert clean.conftests == ("/repo/conftest.py",)

    def test_nothing_is_dropped_when_nothing_is_hidden(self):
        raw = suite(passed=(VISIBLE_ID, HIDDEN_ID), stdout_tail="tests/test_hidden.py::test_f2p PASSED")

        clean = visible_probe(raw, ())

        assert clean.passed == raw.passed
        assert clean.stdout_tail == raw.stdout_tail

    def test_the_output_tail_is_kept_when_it_names_no_hidden_path(self):
        raw = suite(stdout_tail="E   AssertionError in tests/test_app.py:12")

        assert visible_probe(raw, HIDDEN).stdout_tail == raw.stdout_tail

    def test_the_whole_tail_goes_when_it_names_a_hidden_path(self):
        raw = suite(stdout_tail="FAILED tests/test_hidden.py::test_f2p - assert f(9) == 11\nE   AssertionError")

        assert visible_probe(raw, HIDDEN).stdout_tail == ""

    @pytest.mark.parametrize(
        ("raw", "category"),
        [
            ("container runtime unavailable: ...cannot connect to /run/docker.sock", "container runtime unavailable"),
            ("environment build failed for acme/sample (exit 1): ...tests/test_hidden.py", "environment build failed"),
            ("sandbox exceeded 1800.0s running python -m pytest...", "sandbox exceeded"),
            ("  Sandbox Exceeded 5s", "sandbox exceeded"),
        ],
    )
    def test_an_error_is_reduced_to_its_category_prefix(self, raw, category):
        assert visible_probe(suite(error=raw), HIDDEN).error == category

    def test_an_error_of_no_known_kind_is_reduced_to_a_neutral_sentence(self):
        clean = visible_probe(suite(error="report claims 3 failures: tests/test_hidden.py::test_x"), HIDDEN)

        assert clean.error == "sandbox run failed"

    def test_no_error_stays_no_error(self):
        assert visible_probe(suite(), HIDDEN).error is None

    def test_exit_code_and_duration_are_kept(self):
        clean = visible_probe(suite(exit_code=1, duration_seconds=2.5), HIDDEN)

        assert (clean.exit_code, clean.duration_seconds) == (1, 2.5)

    def test_the_fingerprint_is_not_carried(self):
        """Built from an empty result: a field nobody has reviewed is dropped, not forwarded."""
        assert visible_probe(suite(fingerprint={"rootdir": "/repo"}), HIDDEN).fingerprint == {}

    def test_the_result_is_a_new_object_the_input_cannot_reach(self):
        raw = suite(passed=(VISIBLE_ID,))

        assert visible_probe(raw, HIDDEN) is not raw


class TestAgreesWithTheFeedbackFilter:
    """The pipeline's path rule is a copy of `repolace_agents.feedback`'s, kept by a test.

    Each side is one `posixpath` call; importing the other's private helper would be the
    worse coupling. So this pins them equal over the spellings a run can produce, and a
    change to either fails here instead of drifting.
    """

    PATHS = [
        "tests/test_hidden.py::t", "./tests/test_hidden.py::t", "tests//test_hidden.py::t",
        "/tests/test_hidden.py::t", "tests/test_hidden.py", "tests/test_hidden.py::t[a::b]",
        "tests/sub/test_other.py::t", "other/test_hidden.py::t", "tests/test_hidden_x.py::t",
        "hidden_dir/a.py::t", "hidden_dir/deep/b.py::t", "hidden_dirs/c.py::t", "tests\\test_hidden.py::t",
        "", "::t", ".",
    ]

    @pytest.mark.parametrize("hidden", [{"tests/test_hidden.py"}, {"hidden_dir"}, {"./tests/test_hidden.py", "hidden_dir/"}, set()])
    def test_the_same_ids_survive(self, hidden):
        raw = SuiteResult(passed=tuple(self.PATHS), collect_failures=tuple(self.PATHS))

        ours = visible_probe(raw, hidden)
        theirs = feedback._filter_result(raw, feedback._hidden_set(hidden))

        assert ours.passed == theirs.passed
        assert ours.collect_failures == theirs.collect_failures

    CONFTESTS = [
        "/repo/conftest.py", "/repo/tests/conftest.py", "/repo/hidden_dir/conftest.py",
        "/repo/hidden_dir/deep/conftest.py", "/repo/hidden_dirs/conftest.py", "hidden_dir/conftest.py",
    ]

    @pytest.mark.parametrize("hidden", [{"hidden_dir"}, {"tests/conftest.py"}, {"hidden_dir/", "tests/test_hidden.py"}, set()])
    def test_the_same_conftests_survive_including_absolute_container_paths(self, hidden):
        raw = SuiteResult(conftests=tuple(self.CONFTESTS))

        ours = visible_probe(raw, hidden)
        theirs = feedback._filter_result(raw, feedback._hidden_set(hidden))

        assert ours.conftests == theirs.conftests


class TestProtectedCheck:
    """A write refusal must not be an existence oracle for a hidden test file."""

    BASELINE = SuiteResult(
        collected_files=("checks/check_visible.py", "checks/check_hidden.py", "tests/test_app.py"),
        conftests=("/repo/conftest.py", "/repo/checks/conftest.py"),
    )

    def test_a_visible_collected_file_is_protected(self):
        assert protected_check(self.BASELINE, {"checks/check_hidden.py"})("checks/check_visible.py") is True

    def test_a_hidden_collected_file_is_not_protected_so_a_refusal_cannot_reveal_it(self):
        assert protected_check(self.BASELINE, {"checks/check_hidden.py"})("checks/check_hidden.py") is False

    def test_the_raw_baseline_would_have_protected_it_which_is_the_leak(self):
        assert protected_check(self.BASELINE, ())("checks/check_hidden.py") is True

    def test_the_path_heuristic_still_protects_what_it_always_did(self):
        is_protected = protected_check(self.BASELINE, {"checks/check_hidden.py"})

        assert is_protected("tests/test_new.py") is True
        assert is_protected("conftest.py") is True
        assert is_protected("pyproject.toml") is True
        assert is_protected(".github/workflows/ci.yml") is True

    def test_an_ordinary_source_file_is_writable(self):
        assert protected_check(self.BASELINE, HIDDEN)("src/app.py") is False

    def test_a_data_file_beside_a_visible_collected_test_is_protected_but_not_beside_a_hidden_one(self):
        is_protected = protected_check(self.BASELINE, {"checks/check_hidden.py"})

        assert is_protected("checks/golden.json") is True, "checks/check_visible.py is collected from there"
        only_hidden = SuiteResult(collected_files=("hidden_only/check.py",))
        assert protected_check(only_hidden, {"hidden_only/check.py"})("hidden_only/golden.json") is False


@pytest.fixture
async def workspace(origin_url):
    async with task_workspace("acme", "sample", "main", clone_url=origin_url) as ws:
        yield ws


async def prepared_verifier(workspace, backend, *, overlay=None) -> Verifier:
    verifier = Verifier(backend, RepoSpec(key="acme/sample"), TASK_ID, overlay=overlay)
    await verifier.run(workspace, 0)
    return verifier


class TestBuildToolContext:
    async def test_the_checkpoint_is_the_workspaces_commit(self, workspace):
        backend = FakeBackend()
        verifier = await prepared_verifier(workspace, backend)

        ctx = build_tool_context(
            workspace=workspace, verifier=verifier, search=_no_search, hidden_paths=(), baseline=SuiteResult()
        )

        assert ctx.checkpoint == workspace.record_attempt
        assert ctx.checkout == workspace.path

    async def test_a_probe_carries_the_probe_time_limit_to_the_sandbox(self, workspace):
        backend = FakeBackend()
        verifier = await prepared_verifier(workspace, backend)
        ctx = build_tool_context(
            workspace=workspace, verifier=verifier, search=_no_search, hidden_paths=(),
            baseline=SuiteResult(), limits=ToolLimits(max_probe_seconds=42.0),
        )

        await ctx.run_subset(["tests/test_app.py"])

        probe = backend.runs[-1]
        assert probe["spec"].timeout_seconds == 42.0
        assert probe["spec"].test_targets == ("tests/test_app.py",)

    async def test_the_default_probe_limit_is_the_tool_limits_default(self, workspace):
        backend = FakeBackend()
        verifier = await prepared_verifier(workspace, backend)
        ctx = build_tool_context(
            workspace=workspace, verifier=verifier, search=_no_search, hidden_paths=(), baseline=SuiteResult()
        )

        await ctx.run_subset(["tests/test_app.py"])

        assert backend.runs[-1]["spec"].timeout_seconds == ToolLimits().max_probe_seconds

    async def test_a_probe_result_reaches_the_tool_filtered(self, workspace):
        backend = FakeBackend(results=[SuiteResult(passed=("t::base",)), SuiteResult(passed=(VISIBLE_ID, HIDDEN_ID), error=None)])
        verifier = await prepared_verifier(workspace, backend)
        ctx = build_tool_context(
            workspace=workspace, verifier=verifier, search=_no_search, hidden_paths=HIDDEN, baseline=SuiteResult()
        )

        result = await ctx.run_subset(["tests/test_app.py"])

        assert result.passed == (VISIBLE_ID,)

    async def test_a_probe_never_carries_the_overlay_into_its_export(self, workspace):
        """Integration of the two halves: the verifier keeps the answer key out, the filter is the backstop."""
        backend = FakeBackend()
        verifier = await prepared_verifier(workspace, backend, overlay={"tests/test_hidden.py": b"def test_f2p(): ...\n"})
        ctx = build_tool_context(
            workspace=workspace, verifier=verifier, search=_no_search, hidden_paths=verifier.hidden_paths, baseline=SuiteResult()
        )

        await ctx.run_subset(["tests/test_app.py"])

        assert "tests/test_hidden.py" in backend.runs[0]["snapshot"], "the baseline run did carry the overlay"
        assert "tests/test_hidden.py" not in backend.runs[-1]["snapshot"], "the probe must not"

    async def test_a_script_gets_the_tools_timeout_unchanged(self, workspace):
        backend = FakeBackend(scripts=[ScriptResult(exit_code=0, stdout="hi")])
        verifier = await prepared_verifier(workspace, backend)
        ctx = build_tool_context(
            workspace=workspace, verifier=verifier, search=_no_search, hidden_paths=(), baseline=SuiteResult()
        )

        result = await ctx.run_script("print('hi')", 17.0)

        assert result.stdout == "hi"
        assert backend.scripts[0]["timeout"] == 17.0
        assert backend.scripts[0]["script_text"] == "print('hi')"

    async def test_the_write_guard_is_built_from_the_filtered_baseline(self, workspace):
        baseline = SuiteResult(collected_files=("checks/check_hidden.py", "checks/check_visible.py"))
        backend = FakeBackend()
        verifier = await prepared_verifier(workspace, backend)

        ctx = build_tool_context(
            workspace=workspace, verifier=verifier, search=_no_search,
            hidden_paths={"checks/check_hidden.py"}, baseline=baseline,
        )

        assert ctx.is_protected("checks/check_visible.py") is True
        assert ctx.is_protected("checks/check_hidden.py") is False


async def _no_search(query: str, limit: int):
    return ()
