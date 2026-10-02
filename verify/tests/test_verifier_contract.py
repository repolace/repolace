"""`Verifier`'s construction contract: the overlay, `hidden_paths`, and what `run` keeps doing.

What the overlay and the unscored runs *do* is in `test_stage.py`.
"""

import uuid

import pytest

from verify.protocol import RepoSpec
from verify.stage import BASELINE_ATTEMPT, Verifier, VerifierNotReady
from verify.testing import FakeBackend, FakeWorkspace

pytestmark = pytest.mark.anyio

OVERLAY = {"tests/test_hidden.py": b"def test_it():\n    assert False\n", "tests/data/x.json": b"{}"}


def verifier(**kwargs) -> Verifier:
    return Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4(), **kwargs)


@pytest.fixture
def workspace(tmp_path):
    return FakeWorkspace(tmp_path)


class TestWithoutAnOverlay:
    async def test_run_behaves_exactly_as_it_did(self, workspace):
        v = verifier()

        await v.run(workspace, BASELINE_ATTEMPT)
        await v.run(workspace, 1)

        assert workspace.exported == [BASELINE_ATTEMPT, 1]
        assert len(v.backend.prepared) == 1
        assert [run["container"].rsplit("-", 1)[-1] for run in v.backend.runs] == ["0", "1"]

    async def test_a_second_baseline_is_still_refused(self, workspace):
        v = verifier()
        await v.run(workspace, BASELINE_ATTEMPT)

        with pytest.raises(RuntimeError, match="baseline"):
            await v.run(workspace, BASELINE_ATTEMPT)

    async def test_a_dirty_tree_is_refused_not_swallowed_by_the_stage(self, workspace):
        """The export's refusal is the precondition the whole stage rests on; the
        stage must let it through, and must not have built anything by then."""
        v = verifier()
        workspace.set_dirty(True)

        with pytest.raises(RuntimeError, match="refusing to export"):
            await v.run(workspace, BASELINE_ATTEMPT)

        assert v.backend.prepared == [] and v.backend.runs == [] and not v.prepared

    async def test_an_empty_overlay_is_no_overlay(self, workspace):
        v = verifier(overlay={})

        await v.run(workspace, BASELINE_ATTEMPT)

        assert v.hidden_paths == frozenset()

    def test_hidden_paths_is_empty_by_default(self):
        assert verifier().hidden_paths == frozenset()


class TestHiddenPaths:
    def test_they_are_the_overlays_keys(self):
        assert verifier(overlay=OVERLAY).hidden_paths == frozenset(OVERLAY)

    def test_they_are_a_frozenset(self):
        assert isinstance(verifier(overlay=OVERLAY).hidden_paths, frozenset)

    def test_the_caller_cannot_change_them_afterwards(self):
        """The feedback filter trusts this for the whole task, so mutating the
        dict that was passed in must not change what counts as hidden."""
        overlay = dict(OVERLAY)
        v = verifier(overlay=overlay)

        overlay["tests/test_smuggled.py"] = b""
        del overlay["tests/data/x.json"]

        assert v.hidden_paths == frozenset(OVERLAY)

    def test_the_stored_overlay_cannot_be_mutated_through_the_verifier(self):
        v = verifier(overlay=OVERLAY)

        with pytest.raises(TypeError):
            v.overlay["tests/test_smuggled.py"] = b""  # type: ignore[index]

    def test_the_overlay_is_keyword_only(self):
        with pytest.raises(TypeError):
            Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4(), OVERLAY)  # type: ignore[misc]


class TestVerifierNotReady:
    def test_it_is_a_runtime_error(self):
        """So a caller that catches `RuntimeError` for the baseline guard does not
        have to know about it, and one that wants it specifically can catch it."""
        assert issubclass(VerifierNotReady, RuntimeError)
