"""`confine`: the one function that turns a model-chosen string into a path.

Each test names a refusal and pins it, because every one of them fails *open*: a
path that gets through is not an error, it is a write the host later acts on.
"""

import os

import pytest

from repolace_agents.tools.base import ToolError
from repolace_agents.tools.paths import MAX_PATH_CHARS, confine, relative_posix
from verify.scoring import is_protected_path

from tools_support import make_checkout


@pytest.fixture
def checkout(tmp_path):
    root = make_checkout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOKEN=hunter2\n")
    os.symlink(outside / "secret.txt", root / "leak_file.py")  # leaf symlink, target outside
    os.symlink(outside, root / "leak_dir")  # parent symlink, target outside
    os.symlink(root / "src", root / "inner_dir")  # parent symlink that lands back INSIDE
    os.symlink(root / "src" / "pkg" / "util.py", root / "inner_file.py")  # leaf symlink that lands inside
    os.symlink(root / "tests", root / "alias_tests")  # in-tree alias of a protected directory
    os.symlink(root / ".git", root / "alias_git")  # in-tree alias of .git
    return root


def read(checkout, path):
    return confine(checkout, path, write=False)


def write(checkout, path, **kwargs):
    return confine(checkout, path, write=True, is_protected=kwargs.pop("is_protected", is_protected_path))


class TestOrdinaryPaths:
    def test_a_source_file_is_readable_and_writable(self, checkout):
        assert read(checkout, "src/pkg/core.py") == (checkout / "src/pkg/core.py").resolve()
        assert write(checkout, "src/pkg/core.py") == (checkout / "src/pkg/core.py").resolve()

    def test_a_path_that_does_not_exist_yet_is_accepted(self, checkout):
        # create_file needs this; existence is each tool's own question.
        assert write(checkout, "src/pkg/brand_new/mod.py") == (checkout / "src/pkg/brand_new/mod.py").resolve()

    def test_a_dot_slash_prefix_is_normalised(self, checkout):
        assert relative_posix(checkout, read(checkout, "./src/./pkg/core.py")) == "src/pkg/core.py"

    def test_the_root_is_readable_but_not_writable(self, checkout):
        assert read(checkout, ".") == checkout.resolve()
        with pytest.raises(ToolError, match="repository root"):
            write(checkout, ".")


class TestEscapes:
    @pytest.mark.parametrize("write_mode", [False, True])
    def test_an_absolute_path_is_refused(self, checkout, write_mode):
        with pytest.raises(ToolError, match="absolute"):
            confine(checkout, "/etc/passwd", write=write_mode, is_protected=is_protected_path)

    @pytest.mark.parametrize("path", ["..", "../x.py", "src/../../x.py", "src/pkg/../../../x.py"])
    def test_dotdot_out_of_the_tree_is_refused(self, checkout, path):
        with pytest.raises(ToolError, match="'..' is not allowed"):
            write(checkout, path)

    def test_a_symlinked_file_pointing_outside_is_refused(self, checkout):
        with pytest.raises(ToolError, match="symlink"):
            read(checkout, "leak_file.py")
        with pytest.raises(ToolError, match="symlink"):
            write(checkout, "leak_file.py")

    def test_a_symlinked_parent_pointing_outside_is_refused(self, checkout):
        with pytest.raises(ToolError, match="outside|symlink"):
            read(checkout, "leak_dir/secret.txt")
        with pytest.raises(ToolError, match="outside|symlink"):
            write(checkout, "leak_dir/new.py")

    def test_a_symlink_that_lands_back_inside_is_still_refused(self, checkout):
        # Following it would let the repository choose where the read or write lands.
        with pytest.raises(ToolError, match="symlink"):
            read(checkout, "inner_file.py")
        with pytest.raises(ToolError, match="symlink"):
            read(checkout, "inner_dir/pkg/core.py")
        with pytest.raises(ToolError, match="symlink"):
            write(checkout, "inner_dir/pkg/core.py")

    def test_an_alias_of_a_protected_directory_cannot_be_used_to_reach_it(self, checkout):
        with pytest.raises(ToolError, match="symlink"):
            write(checkout, "alias_tests/test_new.py")


class TestGitFamily:
    @pytest.mark.parametrize(
        "path",
        [
            ".git",
            ".git/config",
            ".git/hooks/post-commit",
            ".git/hooks",
            ".GIT/hooks/post-commit",
            ".Git/config",
            ".gitattributes",
            ".gitmodules",
            ".gitignore",
            ".gitkeep",
            "vendor/pkg/.git/config",
            "./.git/config",
        ],
    )
    @pytest.mark.parametrize("write_mode", [False, True])
    def test_every_path_in_the_dot_git_family_is_refused(self, checkout, path, write_mode):
        with pytest.raises(ToolError, match=r"\.git"):
            confine(checkout, path, write=write_mode, is_protected=lambda rel: False)

    @pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".github", ".github/CODEOWNERS"])
    def test_github_directory_is_refused_for_writing(self, checkout, path):
        with pytest.raises(ToolError):
            write(checkout, path)

    def test_github_directory_is_refused_for_writing_even_if_the_guard_would_allow_it(self, checkout):
        # The `.git*` family check is independent of `ctx.is_protected`.
        with pytest.raises(ToolError, match=r"\.git"):
            confine(checkout, ".github/workflows/ci.yml", write=True, is_protected=lambda rel: False)

    def test_a_symlink_onto_dot_git_is_refused(self, checkout):
        with pytest.raises(ToolError, match="symlink"):
            write(checkout, "alias_git/hooks/post-commit")

    def test_a_name_that_merely_contains_git_is_fine(self, checkout):
        assert read(checkout, "src/pkg/digit.py").name == "digit.py"
        assert read(checkout, "src/pkg/legit/.hidden").name == ".hidden"


class TestProtectedWrites:
    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_core.py",
            "tests/helpers.py",
            "tests/data/golden.json",
            "conftest.py",
            "src/pkg/conftest.py",
            "pyproject.toml",
            "setup.cfg",
            "tox.ini",
            "pytest.ini",
            "test_new.py",
            "src/pkg/test_new.py",
            "tests/test_new.py",
            "src/pkg/core_test.py",
            "./tests/test_core.py",
        ],
    )
    def test_test_and_config_files_cannot_be_written(self, checkout, path):
        with pytest.raises(ToolError) as caught:
            write(checkout, path)
        assert "adding new test files is not allowed" in str(caught.value)
        assert "run_python" in str(caught.value)

    @pytest.mark.parametrize("path", ["tests/test_core.py", "conftest.py", "pyproject.toml", "tests/test_new.py"])
    def test_the_same_files_can_be_read(self, checkout, path):
        assert read(checkout, path).name == os.path.basename(path)

    def test_the_guard_is_judged_on_the_resolved_path(self, checkout):
        seen = []
        confine(checkout, "./src/./pkg/core.py", write=True, is_protected=lambda rel: seen.append(rel) or False)
        assert seen == ["src/pkg/core.py"]

    def test_a_baseline_aware_guard_is_honoured(self, checkout):
        # pytest collects `checks/` through a custom python_files; only the pipeline's closure knows.
        def guard(rel: str) -> bool:
            return rel.startswith("checks/") or is_protected_path(rel)

        with pytest.raises(ToolError, match="read-only"):
            write(checkout, "checks/check_foo.py", is_protected=guard)
        assert write(checkout, "src/pkg/core.py", is_protected=guard)

    def test_a_write_without_a_guard_is_a_programming_error_not_a_weaker_check(self, checkout):
        with pytest.raises(TypeError, match="is_protected"):
            confine(checkout, "src/pkg/core.py", write=True)


class TestMalformedPaths:
    @pytest.mark.parametrize(
        "path",
        ["", "a\0b", "x" * (MAX_PATH_CHARS + 1), "src/" + "y" * 300 + "/z.py", "src/\0"],
    )
    @pytest.mark.parametrize("write_mode", [False, True])
    def test_unusable_paths_are_model_errors_not_crashes(self, checkout, path, write_mode):
        # NUL gives ValueError and a long component OSError inside `resolve`; neither
        # may escape as an exception, which `ToolBox` would treat as a bug.
        with pytest.raises(ToolError):
            confine(checkout, path, write=write_mode, is_protected=is_protected_path)

    def test_a_refusal_does_not_leak_a_host_path(self, checkout):
        with pytest.raises(ToolError) as caught:
            read(checkout, "leak_file.py")
        assert str(checkout.parent) not in str(caught.value)


class TestDotDot:
    """`..` is refused outright: the literal walk and `resolve()` read it differently."""

    @pytest.mark.parametrize(
        "path",
        [
            "nonexistent/../link_src/pkg/core.py",
            "nonexistent/../link_file",
            "src/../src/pkg/core.py",  # harmless, but one reading of a path is the point
            "a/b/../../src/pkg/core.py",
            "./..",
        ],
    )
    @pytest.mark.parametrize("write_mode", [False, True])
    def test_any_dotdot_component_is_refused_even_when_it_stays_inside(self, checkout, path, write_mode):
        with pytest.raises(ToolError, match="'..' is not allowed"):
            confine(checkout, path, write=write_mode, is_protected=is_protected_path)

    def test_a_name_that_merely_contains_two_dots_is_fine(self, checkout):
        assert read(checkout, "src/pkg/a..b.py").name == "a..b.py"
        assert read(checkout, "src/..hidden/x.py").name == "x.py"


class TestGitAliases:
    @pytest.mark.parametrize(
        "path",
        [
            "git~1/hooks/pc",
            "GIT~1/hooks/pc",
            "src/Git~2/x",
            "git~1",
            ".g‌it/hooks/pc",  # ZERO WIDTH NON-JOINER: HFS+ ignores it, so this is `.git`
            ".‍git/config",
            "﻿.git/config",
            ".git‮/config",
            ".⁪git/config",
        ],
    )
    @pytest.mark.parametrize("write_mode", [False, True])
    def test_names_a_filesystem_would_read_as_dot_git_are_refused(self, checkout, path, write_mode):
        with pytest.raises(ToolError, match=r"\.git"):
            confine(checkout, path, write=write_mode, is_protected=lambda rel: False)

    @pytest.mark.parametrize("name", ["gitlab", "git~", "git~x", "digit", "legit~1"])
    def test_names_that_only_resemble_them_are_fine(self, checkout, name):
        assert read(checkout, f"src/{name}/x.py").name == "x.py"

    @pytest.mark.parametrize("path", ["a\\b", "a\\.git\\config", "a\tb", "a\nb", "a\x1bb", "a\x7fb", "a\rb"])
    @pytest.mark.parametrize("write_mode", [False, True])
    def test_backslashes_and_control_characters_are_refused(self, checkout, path, write_mode):
        with pytest.raises(ToolError, match="not a usable path"):
            confine(checkout, path, write=write_mode, is_protected=is_protected_path)


class TestSymlinkLoops:
    @pytest.fixture
    def looped(self, checkout):
        os.symlink("loop", checkout / "loop")
        os.symlink("loop2b", checkout / "loop2")
        os.symlink("loop2", checkout / "loop2b")
        return checkout

    @pytest.mark.parametrize("path", ["loop", "loop/x", "loop2/x", "loop2b/y/z"])
    @pytest.mark.parametrize("write_mode", [False, True])
    def test_a_symlink_loop_is_a_symlink_refusal_not_a_crash(self, looped, path, write_mode):
        with pytest.raises(ToolError, match="symlink") as caught:
            confine(looped, path, write=write_mode, is_protected=is_protected_path)
        assert str(looped) not in str(caught.value) and str(looped.parent) not in str(caught.value)

    def test_a_loop_that_resolve_alone_would_trip_on_is_still_a_tool_error(self, looped, monkeypatch):
        # The walk reports a loop as a symlink first; this holds the fallback: if `resolve`
        # does raise RuntimeError it must not escape, and its text (a host path) must not either.
        def boom(root, candidate):
            raise RuntimeError(f"Symlink loop from '{looped}/loop'")

        monkeypatch.setattr("repolace_agents.tools.paths.resolve_within", boom)
        with pytest.raises(ToolError) as caught:
            read(looped, "src/pkg/core.py")
        assert str(looped) not in str(caught.value)


class TestDotfileWrites:
    CI_AND_IDE_FILES = [
        ".circleci/config.yml",
        ".buildkite/pipeline.yml",
        ".travis.yml",
        ".drone.yml",
        "azure-pipelines.yml",
        "Jenkinsfile",
        "bitbucket-pipelines.yml",
        "appveyor.yml",
        "cloudbuild.yaml",
        ".devcontainer/devcontainer.json",
        ".vscode/tasks.json",
        ".pre-commit-config.yaml",
        ".husky/pre-commit",
        ".envrc",
        ".pytest.ini",
        "src/pkg/.hidden",
        "src/.cache/x.py",
        "jenkinsfile",
        "AZURE-PIPELINES.YML",
    ]

    @pytest.mark.parametrize("path", CI_AND_IDE_FILES)
    def test_ci_ide_and_hook_files_cannot_be_written(self, checkout, path):
        with pytest.raises(ToolError) as caught:
            write(checkout, path)
        message = str(caught.value)
        assert "never write a path with a component starting with '.'" in message or "off limits" in message
        assert "Reading them is fine" in message or "off limits" in message

    @pytest.mark.parametrize("path", [p for p in CI_AND_IDE_FILES if not p.startswith(".git")])
    def test_the_same_files_can_be_read(self, checkout, path):
        assert read(checkout, path).name == os.path.basename(path)

    @pytest.mark.parametrize("path", ["Makefile", "src/Jenkinsfile", "docs/appveyor.yml", "src/pkg/new_module.py", "scripts/build.sh"])
    def test_ordinary_files_and_non_root_lookalikes_stay_writable(self, checkout, path):
        # Not a ban on build files or on the names elsewhere in the tree: only the root-level
        # CI definitions, which is where those providers look.
        assert write(checkout, path).name == os.path.basename(path)

    def test_the_rule_is_judged_on_the_resolved_path_not_the_spelling(self, checkout):
        with pytest.raises(ToolError, match="never write a path"):
            write(checkout, "./.envrc")
