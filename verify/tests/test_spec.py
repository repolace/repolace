"""Spec loading and the install heuristic.

The theme is `SpecError`'s own docstring: a spec that silently stopped applying
is worse than one that refuses to load, because the symptom arrives weeks later
as an unexplained unscoreable run with nothing pointing back at the typo.
"""

import pytest

from verify.errors import SpecError
from verify.protocol import RepoSpec
from verify.spec import heuristic_install, install_commands, load_specs, resolve_spec, spec_from_mapping


def write(tmp_path, text: str):
    path = tmp_path / "specs.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoading:
    def test_a_missing_file_is_not_an_error(self, tmp_path):
        """Every repo starts uncurated; that is a state, not a misconfiguration."""
        assert load_specs(tmp_path / "absent.toml") == {}

    def test_the_table_name_becomes_the_key(self, tmp_path):
        specs = load_specs(write(tmp_path, '[specs."acme/sample"]\nbase_image = "python:3.11"\n'))

        assert specs["acme/sample"].key == "acme/sample"
        assert specs["acme/sample"].base_image == "python:3.11"

    def test_lists_become_tuples_so_the_spec_stays_hashable(self, tmp_path):
        specs = load_specs(write(tmp_path, '[specs."a/b"]\ninstall = ["pip install -e ."]\n'))

        assert specs["a/b"].install == ("pip install -e .",)

    def test_malformed_toml_is_refused(self, tmp_path):
        with pytest.raises(SpecError):
            load_specs(write(tmp_path, "[specs.\n"))

    def test_an_unknown_field_is_refused_rather_than_ignored(self):
        with pytest.raises(SpecError, match="unknown field"):
            spec_from_mapping("a/b", {"base_imgae": "python:3.12-slim"})

    def test_the_key_cannot_be_overridden_from_inside_the_table(self):
        """Otherwise a table could disagree with its own heading."""
        with pytest.raises(SpecError, match="unknown field"):
            spec_from_mapping("a/b", {"key": "someone/else"})

    @pytest.mark.parametrize(
        "field,value",
        [
            ("install", "pip install -e ."),  # a bare string, not a list
            ("install", [1]),
            ("keep_addopts", "yes"),
            ("timeout_seconds", "600"),
            ("timeout_seconds", 0),
            ("extra_env", ["A=1"]),
            ("base_image", 3),
        ],
    )
    def test_a_wrong_type_is_refused(self, field, value):
        with pytest.raises(SpecError):
            spec_from_mapping("a/b", {field: value})

    def test_a_bool_is_not_a_number(self):
        """`isinstance(True, int)` is True in Python, and a `timeout_seconds = true`
        that quietly became 1.0 second would fail every suite as a timeout."""
        with pytest.raises(SpecError):
            spec_from_mapping("a/b", {"timeout_seconds": True})

    def test_every_dataclass_field_is_settable(self):
        """The allowed set is derived from the dataclass, so a field added to
        `RepoSpec` cannot be left behind here being rejected as unknown."""
        raw = {
            "base_image": "python:3.11-slim",
            "install": ["pip install -e ."],
            "system_packages": ["gcc"],
            "python_executable": "python3",
            "test_targets": ["tests"],
            "extra_pytest_args": ["-x"],
            "keep_addopts": True,
            "disable_plugin_autoload": True,
            "repo_readonly": True,
            "timeout_seconds": 900,
            "extra_env": {"TZ": "UTC"},
        }
        spec = spec_from_mapping("a/b", raw)

        assert spec.timeout_seconds == 900.0
        assert spec.extra_env == {"TZ": "UTC"}


class TestReservedEnvironment:
    @pytest.mark.parametrize("name", ["PYTHONPATH", "HOME", "REPOLACE_REPORT_PATH", "REPOLACE_RUN_NONCE"])
    def test_a_spec_cannot_set_a_variable_the_sandbox_owns(self, name):
        with pytest.raises(SpecError, match=name):
            spec_from_mapping("a/b", {"extra_env": {name: "/x", "TZ": "UTC"}})

    def test_the_error_names_the_entry(self, tmp_path):
        path = write(tmp_path, '[specs."acme/sample"]\nextra_env = { PYTHONPATH = "/x" }\n')

        with pytest.raises(SpecError, match=r"acme/sample\.extra_env"):
            load_specs(path)

    def test_ordinary_variables_are_untouched(self):
        assert spec_from_mapping("a/b", {"extra_env": {"TZ": "UTC"}}).extra_env == {"TZ": "UTC"}


class TestEnvironmentNamesAreNames:
    @pytest.mark.parametrize("name", ["HOME=/evil", "REPOLACE_RUN_NONCE=known#", "A=B", "PYTHONPATH=/x:"])
    def test_a_name_that_smuggles_an_assignment_is_refused(self, name):
        """`-e HOME=/evil=v` is applied after the sandbox's own HOME and wins; the
        exact-key reserved check cannot see it."""
        with pytest.raises(SpecError, match="invalid variable name"):
            spec_from_mapping("a/b", {"extra_env": {name: "v"}})

    @pytest.mark.parametrize("name", ["", "1A", "A B", "A-B", "A\n", "HOME ", "ÄB"])
    def test_anything_that_is_not_a_plain_identifier_is_refused(self, name):
        with pytest.raises(SpecError):
            spec_from_mapping("a/b", {"extra_env": {name: "v"}})

    def test_the_error_names_the_entry_and_the_offender(self, tmp_path):
        path = write(tmp_path, '[specs."acme/sample"]\nextra_env = { "HOME=/evil" = "v" }\n')

        with pytest.raises(SpecError, match=r"acme/sample\.extra_env.*HOME=/evil"):
            load_specs(path)

    def test_plain_identifiers_are_accepted(self):
        assert spec_from_mapping("a/b", {"extra_env": {"TZ": "UTC", "_X": "1"}}).extra_env == {"TZ": "UTC", "_X": "1"}


class TestResolving:
    def test_an_uncurated_repo_gets_a_default_carrying_its_own_key(self):
        """Falling back rather than raising: an uncurated repo should get a run
        and a diagnosable build failure, not a refusal."""
        assert resolve_spec({}, "a/b") == RepoSpec(key="a/b")

    def test_a_curated_spec_wins(self):
        curated = RepoSpec(key="a/b", base_image="python:3.9")

        assert resolve_spec({"a/b": curated}, "a/b") is curated


class TestHeuristic:
    def test_a_project_gets_an_editable_install(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

        assert "pip install -e ." in heuristic_install(tmp_path)

    def test_requirements_are_installed_before_the_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "requirements.txt").write_text("attrs\n")
        commands = heuristic_install(tmp_path)

        assert commands.index("pip install -r 'requirements.txt'") < commands.index(
            "pip install -e ."
        )

    def test_pytest_is_always_installed_last(self, tmp_path):
        """A repo that assumes pytest is simply present on the machine is common,
        and would otherwise fail with a usage error carrying no clue why."""
        assert heuristic_install(tmp_path)[-1] == "pip install pytest"

    def test_an_empty_directory_still_produces_a_runnable_environment(self, tmp_path):
        assert heuristic_install(tmp_path) == ("pip install pytest",)

    def test_a_curated_install_suppresses_the_heuristic_entirely(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        spec = RepoSpec(key="a/b", install=("make test-deps",))

        assert install_commands(spec, tmp_path) == ("make test-deps",)


def test_generated_files_are_a_table_of_strings():
    spec = spec_from_mapping("a/b", {"generated_files": {"src/p/_version.py": "version = '1'\n"}})

    assert spec.generated_files == {"src/p/_version.py": "version = '1'\n"}


@pytest.mark.parametrize("value", [["a"], {"a": 1}, {1: "a"}, "a"])
def test_generated_files_refuse_anything_else(value):
    with pytest.raises(SpecError, match="generated_files must be a table of strings"):
        spec_from_mapping("a/b", {"generated_files": value})
