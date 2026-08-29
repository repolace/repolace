"""Tests for repository indexing.

The security half of this file exists because a registered repository is
untrusted input to a filesystem walk. git tracks a symlink as an ordinary
mode-120000 entry and clones it back verbatim, so committing
`settings.py -> /home/worker/.env` is the entire attack: the indexer reads
through it, the content lands in `code_chunks.content`, and from there it is
retrievable and reaches an LLM prompt at a third-party API.

Nothing here touches a database or an embedding model -- these are the
filesystem and git layers only.
"""

import subprocess
from pathlib import Path

import pytest

from repolace_shared.git.repo import sanitized_git_env
from retrieval.index import (
    MAX_SOURCE_BYTES,
    _git,
    chunk_file,
    find_python_files,
)

from rag_support import git, write


class TestFindPythonFiles:
    def test_finds_python_files_and_ignores_the_rest(self, checkout):
        found = {p.relative_to(checkout).as_posix() for p in find_python_files(checkout)}

        assert found == {"src/app.py", "pkg/mod.py"}

    def test_ignored_directories_are_pruned(self, checkout):
        """__pycache__ holds stale copies of real modules; indexing them would
        return a hit for code that is not what the repository ships."""
        found = {p.relative_to(checkout).as_posix() for p in find_python_files(checkout)}

        assert "__pycache__/stale.py" not in found

    def test_a_symlinked_python_file_is_not_collected(self, checkout, outside):
        """The reproduction. os.walk lists a symlinked *file* in `filenames`
        even with followlinks=False, and read_text then resolves it."""
        (checkout / "settings.py").symlink_to(outside)

        found = {p.name for p in find_python_files(checkout)}

        assert "settings.py" not in found

    def test_a_symlinked_directory_is_not_descended(self, checkout, tmp_path):
        """Pins followlinks=False, which is a default a later refactor could
        flip without any test objecting."""
        elsewhere = write(tmp_path / "elsewhere" / "hidden.py", "SECRET = 1\n").parent
        (checkout / "linked").symlink_to(elsewhere, target_is_directory=True)

        found = {p.name for p in find_python_files(checkout)}

        assert "hidden.py" not in found

    def test_an_empty_result_warns(self, tmp_path):
        """A repo in another language indexes to zero chunks, which otherwise
        looks exactly like a successful index."""
        empty = tmp_path / "go-repo"
        write(empty / "main.go", "package main\n")

        assert find_python_files(empty) == []


class TestChunkFile:
    def test_chunks_an_ordinary_file(self, checkout):
        chunks = chunk_file(checkout, checkout / "src" / "app.py")

        assert chunks
        assert all(c.file_path == "src/app.py" for c in chunks)

    def test_a_symlink_out_of_the_repo_yields_no_chunks_and_reads_nothing(self, checkout, outside):
        """The live path: `_incremental_index` builds `repo_path / rel` straight
        from `git diff --name-only` and never goes through find_python_files, so
        the walk-level skip does not cover it."""
        link = checkout / "escape.py"
        link.symlink_to(outside)

        chunks = chunk_file(checkout, link)

        assert chunks == []
        assert "host-only-secret" not in "".join(c.content for c in chunks)

    def test_a_path_climbing_out_of_the_repo_yields_no_chunks(self, checkout, outside):
        chunks = chunk_file(checkout, checkout / ".." / "outside" / "secrets.env")

        assert chunks == []

    def test_the_stored_path_is_the_lexical_relative_path(self, tmp_path):
        """The join key `_incremental_index` deletes on must equal what git
        emitted. Resolving it would silently stop matching stored rows for any
        checkout reached through a symlinked directory -- which is what a temp
        dir on macOS is."""
        real = tmp_path / "real"
        write(real / "src" / "app.py", "VALUE = 1\n")
        via_link = tmp_path / "link"
        via_link.symlink_to(real, target_is_directory=True)

        chunks = chunk_file(via_link, via_link / "src" / "app.py")

        assert [c.file_path for c in chunks] == ["src/app.py"]

    def test_an_unreadable_file_does_not_abort_the_index(self, checkout):
        chunks = chunk_file(checkout, checkout / "src" / "does_not_exist.py")

        assert chunks == []

    def test_an_oversized_file_is_skipped(self, checkout):
        """read_text has no bound of its own, and the tree is untrusted."""
        write(checkout / "generated.py", "# pad\n" * (MAX_SOURCE_BYTES // 6 + 10))

        assert chunk_file(checkout, checkout / "generated.py") == []


class TestGitWrapper:
    def test_the_service_environment_is_not_handed_to_git(self, monkeypatch):
        """This wrapper used to inherit all of os.environ, so git and anything
        git spawned held the GitHub App private key -- which mints installation
        tokens for every installation and survives token rotation."""
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", "SUPER-SECRET-APP-KEY")
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@db/repolace")

        dumped = "\n".join(f"{k}={v}" for k, v in sanitized_git_env().items())

        assert "SUPER-SECRET-APP-KEY" not in dumped
        assert "hunter2" not in dumped

    def test_a_global_config_is_not_read(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", ".")
        home = tmp_path / "home"
        write(home / ".gitconfig", "[alias]\n\tboom = status\n")
        monkeypatch.setenv("HOME", str(home))

        with pytest.raises(subprocess.CalledProcessError):
            _git(repo, "boom")
