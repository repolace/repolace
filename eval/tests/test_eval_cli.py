"""`repolace-eval`: a dispatcher that imports a subcommand only when it is chosen."""

from __future__ import annotations

import subprocess
import sys
from types import ModuleType

import pytest

from harness.cli import SUBCOMMANDS, main


def fake_module(result=0, *, record=None):
    module = ModuleType("fake")

    def handler(argv):
        if record is not None:
            record.append(list(argv))
        return result

    module.main = handler
    return module


class TestDispatch:
    def test_the_subcommand_table(self):
        assert {name: module for name, (module, _summary) in SUBCOMMANDS.items()} == {
            "select": "harness.select_instances",
            "fork": "harness.bench_repos",
            "gold": "harness.gold",
            "enqueue": "harness.enqueue",
            "run": "harness.runner",
            "report": "harness.report",
            "retrieval": "harness.retrieval_eval",
        }

    def test_arguments_after_the_subcommand_are_forwarded_and_the_exit_code_returned(self):
        seen: list[list[str]] = []
        imported: list[str] = []

        def importer(name):
            imported.append(name)
            return fake_module(7, record=seen)

        code = main(["run", "--eval-run-id", "r", "-k", "2"], importer=importer)

        assert code == 7
        assert seen == [["--eval-run-id", "r", "-k", "2"]]
        assert imported == ["harness.runner"], "only the chosen subcommand's module is imported"

    def test_a_handler_that_returns_none_exits_zero(self):
        assert main(["gold"], importer=lambda name: fake_module(None)) == 0

    def test_an_unknown_subcommand_is_exit_two(self, capsys):
        assert main(["frobnicate"], importer=lambda name: pytest.fail("must not import")) == 2
        assert "unknown subcommand 'frobnicate'" in capsys.readouterr().err

    def test_no_subcommand_is_exit_two_with_the_usage(self, capsys):
        assert main([], importer=lambda name: pytest.fail("must not import")) == 2
        err = capsys.readouterr().err
        assert "usage: repolace-eval" in err and "enqueue" in err

    def test_an_unmerged_module_is_one_clear_line_and_exit_two(self, capsys):
        def importer(name):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)

        assert main(["select"], importer=importer) == 2
        err = capsys.readouterr().err
        assert err.count("\n") == 1, "one line, not a traceback"
        assert "subcommand 'select' cannot run" in err and "harness.select_instances" in err and "not merged" in err

    def test_a_missing_third_party_dependency_names_it(self, capsys):
        def importer(name):
            raise ModuleNotFoundError("No module named 'pyarrow'", name="pyarrow")

        assert main(["select"], importer=importer) == 2
        err = capsys.readouterr().err
        assert err.count("\n") == 1 and "pyarrow" in err

    def test_a_module_without_main_is_exit_two(self, capsys):
        assert main(["report"], importer=lambda name: ModuleType(name)) == 2
        assert "no main()" in capsys.readouterr().err

    def test_a_broken_module_that_raises_importerror_is_exit_two(self, capsys):
        def importer(name):
            raise ImportError("cannot import name 'x' from 'y'")

        assert main(["run"], importer=importer) == 2
        assert "cannot run" in capsys.readouterr().err


class TestHelp:
    @pytest.mark.parametrize("flag", ["--help", "-h", "help"])
    def test_help_lists_every_subcommand_and_imports_nothing(self, flag, capsys):
        code = main([flag], importer=lambda name: pytest.fail("--help must not import a subcommand"))

        out = capsys.readouterr().out
        assert code == 0
        assert all(name in out for name in SUBCOMMANDS)

    def test_help_does_not_import_the_heavy_modules(self):
        """In a fresh interpreter: nothing the subcommands pull in is loaded by `--help`."""
        probe = (
            "import sys\n"
            "from harness import cli\n"
            "assert cli.main(['--help']) == 0\n"
            "heavy = ('torch', 'sentence_transformers', 'sqlalchemy', 'httpx', 'litellm', 'langgraph', 'docker',\n"
            "         'harness.runner', 'harness.enqueue', 'harness.bench_repos', 'harness.gold', 'harness.db')\n"
            "print('LOADED:' + ','.join(m for m in heavy if m in sys.modules))\n"
        )
        done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)

        assert done.returncode == 0, done.stderr
        assert done.stdout.strip().splitlines()[-1] == "LOADED:"


class TestRealModules:
    @pytest.mark.parametrize("name", ["fork", "enqueue", "run", "gold"])
    def test_each_subcommand_of_this_stream_imports_and_prints_its_help(self, name, capsys):
        with pytest.raises(SystemExit) as exit_info:
            main([name, "--help"])

        assert exit_info.value.code == 0
        assert f"repolace-eval {name}" in capsys.readouterr().out
