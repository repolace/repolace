"""The stub and gold runners, driven against a real directory and a scripted `verify_attempt`.

The gold runner is the one with a security surface: it writes files named by an instance
file into a checkout the host will commit from. Its tests are mostly refusals, each built as an
`InstanceSpec` directly (the dataclass does not validate paths; loading does), so the runner's
own confinement is what is under test and not the loader's.
"""

import os
from pathlib import Path

import pytest

from repolace_agents.contracts import StopReason
from repolace_pipeline.edit import StubEditRequest
from repolace_pipeline.pr import PLUMBING_SUMMARY_NOTE
from repolace_pipeline.runners import GoldAgent, GoldPatchRefused, StubAgent, write_gold_files

from pipeline_support import TASK_ID, attempt_record, chunk, make_deps, make_instance, request

pytestmark = pytest.mark.anyio

APP = "def parse_config(path):\n    return None\n"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(APP)
    return root


class Verifier:
    """A scripted `verify_attempt` that records the attempt numbers it was asked for."""

    def __init__(self, record):
        self.record = record
        self.asked: list[int] = []

    async def __call__(self, attempt: int):
        self.asked.append(attempt)
        return self.record


class TestStubAgent:
    async def test_it_writes_the_marker_above_the_top_hit_and_verifies_attempt_one(self, checkout):
        verify = Verifier(attempt_record())
        agent = StubAgent(request(chunk(file_path="src/app.py", start_line=1, end_line=2)))

        result = await agent(make_deps(checkout, verify_attempt=verify))

        assert "NOT A FIX" in (checkout / "src" / "app.py").read_text().splitlines()[0]
        assert verify.asked == [1]
        assert result.last_attempt == attempt_record()
        assert (result.attempts, result.steps) == (1, 1)

    async def test_it_never_submits_so_the_product_gate_still_needs_the_opt_in(self, checkout):
        result = await StubAgent(request(chunk(file_path="src/app.py")))(
            make_deps(checkout, verify_attempt=Verifier(attempt_record()))
        )

        assert result.stop_reason is StopReason.STEP_CAP
        assert result.submitted is False

    async def test_its_summary_is_the_plumbing_note(self, checkout):
        result = await StubAgent(request(chunk(file_path="src/app.py")))(
            make_deps(checkout, verify_attempt=Verifier(attempt_record()))
        )

        assert result.summary == PLUMBING_SUMMARY_NOTE

    async def test_an_edit_that_commits_nothing_is_an_error_not_a_no_op(self, checkout):
        """The stub's contract is that it makes a change; none means the repo or repolace is wrong."""
        agent = StubAgent(request(chunk(file_path="src/app.py")))

        with pytest.raises(RuntimeError, match="no committable change"):
            await agent(make_deps(checkout, verify_attempt=Verifier(None)))

    async def test_a_retrieved_path_that_escapes_the_checkout_is_refused(self, checkout, tmp_path):
        outside = tmp_path / "outside.py"
        outside.write_text("x = 1\n")
        agent = StubAgent(request(chunk(file_path=str(outside))))

        with pytest.raises(ValueError, match="not usable"):
            await agent(make_deps(checkout, verify_attempt=Verifier(attempt_record())))

        assert outside.read_text() == "x = 1\n"

    async def test_it_is_built_from_a_request_with_a_task_id(self):
        assert isinstance(StubAgent(request()).request, StubEditRequest)
        assert StubAgent(request()).request.task_id == TASK_ID


class TestGoldAgent:
    async def test_it_writes_the_reference_files_and_verifies_attempt_one(self, checkout):
        verify = Verifier(attempt_record())
        instance = make_instance(gold_files={"src/app.py": "def parse_config(path):\n    return {}\n"})

        result = await GoldAgent(instance)(make_deps(checkout, verify_attempt=verify))

        assert (checkout / "src" / "app.py").read_text() == "def parse_config(path):\n    return {}\n"
        assert verify.asked == [1]
        assert result.stop_reason is StopReason.SUBMITTED
        assert result.submitted is True
        assert result.last_attempt == attempt_record()
        assert result.attempts == 1

    async def test_it_creates_missing_directories_for_a_new_file(self, checkout):
        instance = make_instance(gold_files={"src/new/pkg/mod.py": "X = 1\n"})

        await GoldAgent(instance)(make_deps(checkout, verify_attempt=Verifier(attempt_record())))

        assert (checkout / "src" / "new" / "pkg" / "mod.py").read_text() == "X = 1\n"

    async def test_files_are_written_byte_for_byte_with_no_newline_translation(self, checkout):
        instance = make_instance(gold_files={"src/app.py": "a = 1\r\nb = 2\r\n"})

        await GoldAgent(instance)(make_deps(checkout, verify_attempt=Verifier(attempt_record())))

        assert (checkout / "src" / "app.py").read_bytes() == b"a = 1\r\nb = 2\r\n"

    async def test_a_fix_identical_to_the_base_is_no_change_not_a_submission(self, checkout):
        result = await GoldAgent(make_instance(gold_files={"src/app.py": APP}))(
            make_deps(checkout, verify_attempt=Verifier(None))
        )

        assert result.stop_reason is StopReason.NO_CHANGE
        assert result.last_attempt is None and result.attempts == 0


class TestGoldPathConfinement:
    """Every refusal leaves the tree exactly as it was, and nothing outside it written."""

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/cron.d/x",
            "../escape.py",
            "src/../../escape.py",
            ".git/hooks/post-commit",
            ".git/config",
            ".GIT/hooks/post-commit",
            "src/.git/hooks/post-commit",
            ".gitattributes",
            ".github/workflows/x.yml",
            "a\x00b.py",
            "src\\..\\x.py",
            "",
        ],
    )
    async def test_a_hostile_path_is_refused_before_anything_is_written(self, checkout, tmp_path, path):
        instance = make_instance(gold_files={"src/ok.py": "OK = 1\n", path: "pwned\n"})
        verify = Verifier(attempt_record())

        with pytest.raises(GoldPatchRefused):
            await GoldAgent(instance)(make_deps(checkout, verify_attempt=verify))

        assert not (checkout / "src" / "ok.py").exists(), "the files before the bad one must not have been written"
        assert verify.asked == [], "nothing is verified after a refusal"
        assert not (tmp_path / "escape.py").exists()
        assert (checkout / "src" / "app.py").read_text() == APP

    async def test_a_symlinked_file_is_refused_not_followed(self, checkout, tmp_path):
        target = tmp_path / "host-file"
        target.write_text("host\n")
        os.symlink(target, checkout / "src" / "link.py")

        with pytest.raises(GoldPatchRefused, match="symlink"):
            write_gold_files(checkout, {"src/link.py": "pwned\n"})

        assert target.read_text() == "host\n"

    async def test_a_symlinked_directory_is_refused_not_followed(self, checkout, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, checkout / "linked")

        with pytest.raises(GoldPatchRefused):
            write_gold_files(checkout, {"linked/mod.py": "pwned\n"})

        assert list(outside.iterdir()) == []

    async def test_a_path_that_names_an_existing_directory_is_refused(self, checkout):
        with pytest.raises(GoldPatchRefused, match="directory"):
            write_gold_files(checkout, {"src": "x\n"})

    async def test_the_refusal_names_the_path_it_refused(self, checkout):
        with pytest.raises(GoldPatchRefused, match=r"\.git/hooks/post-commit"):
            write_gold_files(checkout, {".git/hooks/post-commit": "x\n"})

    def test_it_returns_what_it_wrote(self, checkout):
        assert write_gold_files(checkout, {"src/a.py": "A = 1\n", "src/b.py": "B = 1\n"}) == ["src/a.py", "src/b.py"]
