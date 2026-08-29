"""Rendering the environment image, and deciding when it has to be rebuilt.

The cache key is the interesting half. It has to be stable across the agent's
edits -- an image rebuilt per attempt would mean the baseline and the attempt
ran in different environments, and `score` would be comparing two things that
differ for a reason it cannot see -- while still changing when a dependency
does.
"""

import pytest

from verify.dockerfile import image_cache_key, image_tag, plugin_source, render_dockerfile
from verify.errors import SpecError
from verify.protocol import RepoSpec

INSTALL = ("pip install -e .", "pip install pytest")


@pytest.fixture
def source(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'sample'\n")
    (tmp_path / "sample.py").write_text("def f():\n    return 1\n")
    return tmp_path


class TestRendering:
    def test_the_base_image_is_the_first_instruction(self):
        text = render_dockerfile(RepoSpec(key="a/b", base_image="python:3.9-slim"), INSTALL)

        assert text.splitlines()[0] == "FROM python:3.9-slim"

    def test_each_install_command_becomes_its_own_run(self):
        text = render_dockerfile(RepoSpec(key="a/b"), INSTALL)

        assert "RUN pip install -e ." in text
        assert "RUN pip install pytest" in text

    def test_no_apt_layer_when_no_system_packages(self):
        """An unconditional `apt-get update` doubles the build time of every repo
        that does not need one."""
        assert "apt-get" not in render_dockerfile(RepoSpec(key="a/b"), INSTALL)

    def test_the_apt_cache_is_deleted_in_the_same_layer_it_is_created(self):
        text = render_dockerfile(RepoSpec(key="a/b", system_packages=("gcc",)), INSTALL)

        assert text.count("RUN apt-get update") == 1
        assert "rm -rf /var/lib/apt/lists/*" in text

    def test_system_packages_are_shell_quoted(self):
        text = render_dockerfile(RepoSpec(key="a/b", system_packages=("lib; rm -rf /",)), INSTALL)

        assert "'lib; rm -rf /'" in text

    def test_the_plugin_is_copied_before_the_source(self):
        """Least-changing layer first: an edit to the tree must not invalidate it."""
        text = render_dockerfile(RepoSpec(key="a/b"), INSTALL)

        assert text.index("COPY plugin/") < text.index("COPY source/")

    def test_there_is_no_cmd_so_the_pytest_argv_stays_in_build_run_argv(self):
        text = render_dockerfile(RepoSpec(key="a/b"), INSTALL)

        assert "CMD" not in text
        assert "ENTRYPOINT" not in text

    @pytest.mark.parametrize("command", ["pip install -e .\nRUN evil", "   "])
    def test_a_command_that_is_not_one_line_is_refused(self, command):
        """A newline inside a RUN turns the remainder into a fresh instruction --
        usually a syntax error, but 'usually' is what makes it worth refusing."""
        with pytest.raises(SpecError):
            render_dockerfile(RepoSpec(key="a/b"), (command,))


class TestCacheKey:
    def test_editing_source_does_not_change_the_key(self, source):
        """The whole point: baseline and attempt must share one environment."""
        before = image_cache_key(RepoSpec(key="a/b"), INSTALL, source)
        (source / "sample.py").write_text("def f():\n    return 2\n")

        assert image_cache_key(RepoSpec(key="a/b"), INSTALL, source) == before

    def test_editing_a_manifest_changes_the_key(self, source):
        before = image_cache_key(RepoSpec(key="a/b"), INSTALL, source)
        (source / "pyproject.toml").write_text("[project]\nname = 'sample'\ndependencies = ['attrs']\n")

        assert image_cache_key(RepoSpec(key="a/b"), INSTALL, source) != before

    def test_deleting_a_manifest_changes_the_key(self, source):
        """A repo that removes its requirements file has changed what gets
        installed just as much as one that edits it."""
        (source / "requirements.txt").write_text("attrs\n")
        before = image_cache_key(RepoSpec(key="a/b"), INSTALL, source)
        (source / "requirements.txt").unlink()

        assert image_cache_key(RepoSpec(key="a/b"), INSTALL, source) != before

    def test_changing_the_base_image_changes_the_key(self, source):
        a = image_cache_key(RepoSpec(key="a/b"), INSTALL, source)
        b = image_cache_key(RepoSpec(key="a/b", base_image="python:3.9-slim"), INSTALL, source)

        assert a != b

    def test_changing_the_install_commands_changes_the_key(self, source):
        a = image_cache_key(RepoSpec(key="a/b"), INSTALL, source)
        b = image_cache_key(RepoSpec(key="a/b"), ("pip install .",), source)

        assert a != b

    def test_a_missing_source_directory_still_produces_a_key(self, tmp_path):
        """Every manifest reads as absent, which is a valid state, not an error."""
        assert image_cache_key(RepoSpec(key="a/b"), INSTALL, tmp_path / "nope")


class TestTag:
    def test_the_slash_in_a_repo_key_is_replaced(self):
        """Docker tag components allow `[a-zA-Z0-9_.-]` and nothing else."""
        tag = image_tag("repolace-verify", RepoSpec(key="acme/sample"), "deadbeef")

        assert tag == "repolace-verify:acme_sample-deadbeef"


def test_the_plugin_is_findable_and_is_not_imported():
    """Importing it would register its hooks into repolace's own test run."""
    import sys

    assert plugin_source().is_file()
    assert "_repolace_report" not in sys.modules
