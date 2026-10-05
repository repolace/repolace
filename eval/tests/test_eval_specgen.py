"""`harness.specgen`: a snapshot entry becomes a `RepoSpec` mapping, or a loud refusal.

The table is a fixture (`eval_support.swebench_table`), not SWE-bench's constants:
this module ports none.
"""

import json

import pytest

from eval_support import swebench_table, write_specs_file
from harness.specgen import (
    SpecgenError,
    environment_rejection,
    load_swebench_specs,
    needs_system_packages,
    python_version,
    spec_for,
)
from verify.spec import spec_from_mapping


def one(entry: dict) -> dict:
    return {"o/r": {"1.0": entry}}


class TestSpecFor:
    def test_python_version_becomes_the_base_image(self):
        assert spec_for("psf/requests", "2.0", swebench_table())["base_image"] == "python:3.9-slim"

    def test_install_order_is_other_pins_then_install_then_pytest_last(self):
        table = one({
            "python": "3.9",
            "pip_packages": ["pytest==6.2.5", "numpy<1.24", "six"],
            "pre_install": ["echo pre"],
            "install": "python -m pip install -e .[test]",
        })
        assert spec_for("o/r", "1.0", table)["install"] == [
            "pip install 'numpy<1.24' six",
            "echo pre",
            "python -m pip install -e .[test]",
            "pip install pytest==6.2.5",
        ]

    def test_a_version_specifier_is_quoted_so_the_shell_cannot_read_it_as_a_redirect(self):
        install = spec_for("o/r", "1.0", one({"python": "3.9", "pip_packages": ["a>=1,<2"]}))["install"]
        assert install[0] == "pip install 'a>=1,<2'"

    def test_with_no_pytest_pin_a_bare_pytest_install_is_last(self):
        install = spec_for("o/r", "1.0", one({"python": "3.9", "install": "pip install -e ."}))["install"]
        assert install == ["pip install -e .", "pip install pytest"]

    @pytest.mark.parametrize("pin", ["Pytest>=6", "pytest[testing]==7.0", "pytest"])
    def test_every_spelling_of_a_pytest_requirement_is_held_back_until_last(self, pin):
        install = spec_for("o/r", "1.0", one({"python": "3.9", "pip_packages": [pin, "six"]}))["install"]
        assert install[-1] == f"pip install {pin}" or install[-1] == f"pip install '{pin}'"
        assert install[0] == "pip install six"

    def test_a_pytest_plugin_is_not_a_pytest_pin(self):
        install = spec_for("o/r", "1.0", one({"python": "3.9", "pip_packages": ["pytest-cov==4"]}))["install"]
        assert install == ["pip install pytest-cov==4", "pip install -e .", "pip install pytest"]

    def test_a_missing_install_defaults_to_the_editable_install(self):
        assert "pip install -e ." in spec_for("o/r", "1.0", one({"python": "3.9"}))["install"]

    def test_requirements_txt_is_installed_first(self):
        table = one({"python": "3.9", "packages": "requirements.txt", "pip_packages": ["six"]})
        assert spec_for("o/r", "1.0", table)["install"][:2] == ["pip install -r requirements.txt", "pip install six"]

    def test_a_repository_without_a_tracked_requirements_file_installs_its_test_requirements_instead(self):
        entry = {"python": "3.9", "packages": "requirements.txt"}

        pylint = spec_for("pylint-dev/pylint", "2.9", {"pylint-dev/pylint": {"2.9": entry}})["install"]
        flask = spec_for("pallets/flask", "2.3", {"pallets/flask": {"2.3": entry}})["install"]

        assert pylint[0] == "pip install -r requirements_test_min.txt"
        assert flask[0] == "pip install -r requirements/tests.txt"

    def test_a_git_versioned_distribution_is_given_its_version_for_the_editable_install_only(self):
        entry = {"python": "3.9", "install": "python -m pip install -e .", "pip_packages": ["attrs==23.1.0"]}

        install = spec_for("pytest-dev/pytest", "5.2", {"pytest-dev/pytest": {"5.2": entry}})["install"]

        assert install == [
            "pip install attrs==23.1.0",
            "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYTEST=5.2 python -m pip install -e .",
            "pip install pytest",
        ]

    def test_a_git_versioned_distribution_also_gets_its_generated_file_laid_over_each_export(self):
        entry = {"python": "3.9", "install": "python -m pip install -e ."}

        mapping = spec_for("pytest-dev/pytest", "5.2", {"pytest-dev/pytest": {"5.2": entry}})

        assert mapping["generated_files"] == {"src/_pytest/_version.py": "version = '5.2'\n"}
        assert spec_from_mapping("pytest-dev/pytest", mapping).generated_files == mapping["generated_files"]

    def test_sphinx_gets_roman_with_the_other_pins_before_the_project_install(self):
        entry = {"python": "3.9", "pip_packages": ["Jinja2==3.0.3"], "install": "python -m pip install -e .[test]"}

        install = spec_for("sphinx-doc/sphinx", "3.1", {"sphinx-doc/sphinx": {"3.1": entry}})["install"]

        assert install[0] == "pip install Jinja2==3.0.3 roman"
        assert install.index("python -m pip install -e .[test]") > 0

    def test_other_repositories_get_no_pretend_version(self):
        install = spec_for("o/r", "1.0", one({"python": "3.9", "install": "pip install -e ."}))["install"]

        assert not any("PRETEND_VERSION" in command for command in install)
        assert "generated_files" not in spec_for("o/r", "1.0", one({"python": "3.9"}))

    def test_apt_packages_become_system_packages(self):
        assert spec_for("psf/requests", "0.1", swebench_table())["system_packages"] == ["libffi-dev"]

    def test_no_system_packages_key_when_there_are_none(self):
        assert "system_packages" not in spec_for("psf/requests", "2.0", swebench_table())

    def test_the_swebench_test_command_is_not_ported(self):
        mapping = spec_for("psf/requests", "2.0", swebench_table())
        assert set(mapping) <= {"base_image", "install", "system_packages"}

    def test_the_output_round_trips_through_spec_from_mapping_and_json(self):
        mapping = spec_for("psf/requests", "0.1", swebench_table())
        assert json.loads(json.dumps(mapping)) == mapping
        spec = spec_from_mapping("psf__requests-1", mapping)
        assert spec.key == "psf__requests-1"
        assert spec.base_image == "python:3.6-slim"
        assert spec.system_packages == ("libffi-dev",)
        assert spec.install == tuple(mapping["install"])


class TestRefusals:
    def test_unknown_repo(self):
        with pytest.raises(SpecgenError, match="repo is not in the SWE-bench spec snapshot"):
            spec_for("nobody/nothing", "1.0", swebench_table())

    def test_unknown_version_names_the_known_ones(self):
        with pytest.raises(SpecgenError, match=r"version is not in the snapshot for psf/requests \(known: 0.1, 0.2, 2.0\)"):
            spec_for("psf/requests", "9.9", swebench_table())

    def test_a_conda_environment_is_refused_not_approximated(self):
        with pytest.raises(SpecgenError, match="needs a conda environment"):
            spec_for("pydata/xarray", "2022.03", swebench_table())

    def test_a_pip_option_is_not_a_requirement(self):
        with pytest.raises(SpecgenError, match="is an option"):
            spec_for("o/r", "1.0", one({"python": "3.9", "pip_packages": ["--pre", "x"]}))

    def test_an_unknown_key_is_refused_so_nothing_is_silently_unported(self):
        with pytest.raises(SpecgenError, match="unknown key.*test_cmd"):
            spec_for("o/r", "1.0", one({"python": "3.9", "test_cmd": "pytest -rA"}))

    @pytest.mark.parametrize("python", ["3", "3.9.1", "py39", 3.9, None])
    def test_a_python_that_is_not_x_dot_y(self, python):
        with pytest.raises(SpecgenError, match="python must be"):
            spec_for("o/r", "1.0", one({"python": python}))

    def test_a_multi_line_command_would_become_two_dockerfile_instructions(self):
        with pytest.raises(SpecgenError, match="spans multiple lines"):
            spec_for("o/r", "1.0", one({"python": "3.9", "pre_install": ["echo a\nRUN evil"]}))

    def test_an_empty_command(self):
        with pytest.raises(SpecgenError, match="empty entry"):
            spec_for("o/r", "1.0", one({"python": "3.9", "pre_install": ["  "]}))

    def test_a_system_package_with_whitespace(self):
        with pytest.raises(SpecgenError, match="whitespace"):
            spec_for("o/r", "1.0", one({"python": "3.9", "apt_pkgs": ["libx libY"]}))


class TestLoader:
    def test_a_valid_snapshot_loads(self, tmp_path):
        path = write_specs_file(tmp_path / "specs.json", swebench_table())
        assert load_swebench_specs(path)["psf/requests"]["2.0"]["python"] == "3.9"

    def test_a_missing_snapshot_says_how_to_make_it_and_is_not_an_empty_table(self, tmp_path):
        with pytest.raises(SpecgenError, match="does not exist.*snippet in the docstring of harness.specgen"):
            load_swebench_specs(tmp_path / "absent.json")

    def test_the_committed_snapshot_loads_and_covers_the_instance_repositories(self):
        # Generated once from swebench==4.1.0 (see the docstring of harness.specgen); every
        # entry has to survive the strict loader, and the repositories the instances come from
        # have to be present, or `select` would report an environment problem for each.
        from harness.specgen import DEFAULT_SPECS_PATH

        table = load_swebench_specs(DEFAULT_SPECS_PATH)

        assert {"pytest-dev/pytest", "sphinx-doc/sphinx", "mwaskom/seaborn"} <= set(table)

    def test_bad_json(self, tmp_path):
        path = tmp_path / "specs.json"
        path.write_text("{not json")
        with pytest.raises(SpecgenError, match="not valid JSON"):
            load_swebench_specs(path)

    def test_a_wrong_schema_version(self, tmp_path):
        path = tmp_path / "specs.json"
        path.write_text(json.dumps({"schema_version": 2, "specs": swebench_table()}))
        with pytest.raises(SpecgenError, match="schema_version is 2"):
            load_swebench_specs(path)

    def test_extra_top_level_keys(self, tmp_path):
        path = tmp_path / "specs.json"
        path.write_text(json.dumps({"schema_version": 1, "specs": swebench_table(), "notes": "x"}))
        with pytest.raises(SpecgenError, match="exactly"):
            load_swebench_specs(path)

    def test_an_empty_table(self, tmp_path):
        path = tmp_path / "specs.json"
        path.write_text(json.dumps({"schema_version": 1, "specs": {}}))
        with pytest.raises(SpecgenError, match="non-empty"):
            load_swebench_specs(path)

    def test_a_bad_entry_is_reported_with_its_repo_and_version(self, tmp_path):
        path = write_specs_file(tmp_path / "specs.json", one({"python": "3.9", "eval_commands": []}))
        with pytest.raises(SpecgenError, match=r"o/r@1.0.*eval_commands"):
            load_swebench_specs(path)


class TestEnvironmentFilter:
    def test_python_version_is_read_from_the_base_image(self):
        assert python_version({"base_image": "python:3.10-slim"}) == (3, 10)

    def test_a_base_image_that_is_not_a_slim_python_image(self):
        with pytest.raises(SpecgenError, match="not a python:X.Y-slim image"):
            python_version({"base_image": "ubuntu:20.04"})

    def test_apt_inside_a_command_counts_as_system_packages(self):
        assert needs_system_packages({"install": ["apt-get update && apt-get install -y gcc", "pip install -e ."]})
        assert needs_system_packages({"install": ["apt install -y gcc"]})
        assert not needs_system_packages({"install": ["pip install -e .", "echo aptitude is a word"]})

    def test_system_packages_count(self):
        assert needs_system_packages({"system_packages": ["libffi-dev"], "install": ["pip install -e ."]})

    def test_old_python_with_system_packages_is_rejected(self):
        spec = spec_for("psf/requests", "0.1", swebench_table())
        assert "archived" in environment_rejection(spec)

    def test_old_python_without_system_packages_is_fine(self):
        assert environment_rejection(spec_for("psf/requests", "0.2", swebench_table())) is None

    def test_new_python_with_system_packages_is_fine(self):
        spec = {"base_image": "python:3.9-slim", "system_packages": ["libffi-dev"], "install": ["pip install -e ."]}
        assert environment_rejection(spec) is None

    def test_the_boundary_is_python_3_8(self):
        assert environment_rejection({"base_image": "python:3.8-slim", "system_packages": ["x"], "install": []}) is None
        assert environment_rejection({"base_image": "python:3.7-slim", "system_packages": ["x"], "install": []}) is not None
