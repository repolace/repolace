"""Database-backed tests for threading the embedding strategy through indexing.

The failure these exist to prevent is quiet. `reindex_if_stale` compares commits,
so before `registered_repos.index_strategy` a strategy change on a repo whose
index was current did nothing, and an incremental pass after one embedded the
changed files one way and left every other chunk the other way. Retrieval quality
then becomes a blend of two experiments, and nothing errors.

The embedder is the shared recording fake, so what is asserted is the *call* --
which strategy, which texts -- and no model is loaded.

Every index operation runs on a session of its own, as the pipeline does
(`run._index`). That is not just fidelity: `reindex_if_stale` reads the repo row
with `db.get`, which hands back a session's already-loaded copy, and a session
that loaded the row before an earlier index wrote it keeps seeing the old values.
"""

import uuid
from pathlib import Path
from typing import NamedTuple

import pytest
from sqlalchemy import select, update

from repolace_shared.db.models import CodeChunk, RegisteredRepo
from retrieval.index import get_current_chunk_count, index_repo, reindex_if_stale
from retrieval.testing import FakeEmbedder, install_fake_embedder

from rag_support import commit_all, init_repo, seed_repo, write

pytestmark = [pytest.mark.anyio, pytest.mark.db]

ALPHA = "def alpha_fn():\n    return 'alpha'\n"
BETA = "def beta_fn():\n    return 'beta'\n"


class Source(NamedTuple):
    path: Path
    sha: str


@pytest.fixture
def source(tmp_path: Path) -> Source:
    """A real git repo, one commit, two files with one function (so one chunk) each."""
    root = init_repo(tmp_path / "repo")
    write(root / "alpha.py", ALPHA)
    write(root / "beta.py", BETA)
    return Source(root, commit_all(root, "first"))


def commit_alpha_change(source: Source) -> str:
    write(source.path / "alpha.py", "def alpha_fn():\n    return 'alpha-changed'\n")
    return commit_all(source.path, "change alpha")


class Index:
    """One registered repo and the checkout it is indexed from, driven session-per-call."""

    def __init__(self, factory, repo_id: uuid.UUID, source: Source):
        self.factory = factory
        self.repo_id = repo_id
        self.source = source

    async def full(self, **kwargs) -> int:
        async with self.factory() as session:
            return await index_repo(session, self.repo_id, self.source.path, self.source.sha, **kwargs)

    async def reindex(self, sha: str | None = None, **kwargs) -> int:
        async with self.factory() as session:
            return await reindex_if_stale(
                session, self.repo_id, self.source.path, sha or self.source.sha, **kwargs
            )

    async def stored(self) -> tuple[str | None, str | None]:
        """(indexed_commit_sha, index_strategy) as the database holds them."""
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(RegisteredRepo.indexed_commit_sha, RegisteredRepo.index_strategy).where(
                        RegisteredRepo.id == self.repo_id
                    )
                )
            ).one()
        return row.indexed_commit_sha, row.index_strategy

    async def chunk_count(self) -> int:
        async with self.factory() as session:
            return await get_current_chunk_count(session, self.repo_id)

    async def chunk_rows(self) -> list[tuple[str, str]]:
        async with self.factory() as session:
            rows = await session.execute(
                select(CodeChunk.file_path, CodeChunk.commit_sha).order_by(CodeChunk.file_path)
            )
            return [tuple(row) for row in rows]

    async def forget_the_strategy(self) -> None:
        """Make this look like an index built before the column existed."""
        async with self.factory() as session:
            await session.execute(
                update(RegisteredRepo).where(RegisteredRepo.id == self.repo_id).values(index_strategy=None)
            )
            await session.commit()


@pytest.fixture
async def ix(db_session_factory, source) -> Index:
    async with db_session_factory() as session:
        repo = await seed_repo(session)
    return Index(db_session_factory, repo.id, source)


class TestStrategyReachesTheEmbedder:
    async def test_a_full_index_passes_the_strategy_to_embed_texts(self, ix, embedder):
        await ix.full(strategy="head_tail")

        assert embedder.text_calls
        assert embedder.strategies_used == {"head_tail"}

    async def test_the_first_reindex_if_stale_passes_the_strategy_to_embed_texts(self, ix, embedder):
        await ix.reindex(strategy="windows")

        assert embedder.text_calls
        assert embedder.strategies_used == {"windows"}

    async def test_an_incremental_index_passes_the_strategy_and_embeds_only_what_changed(self, ix, embedder, source):
        await ix.reindex(strategy="head_tail")
        embedder.reset()
        new_sha = commit_alpha_change(source)

        written = await ix.reindex(new_sha, strategy="head_tail")

        assert written == 1
        assert embedder.strategies_used == {"head_tail"}
        assert len(embedder.embedded_texts) == 1
        assert "alpha-changed" in embedder.embedded_texts[0]

    async def test_the_strategy_is_validated_before_the_database_is_touched(self, ix, embedder):
        with pytest.raises(ValueError, match="unknown strategy"):
            await ix.full(strategy="nonsense")
        with pytest.raises(ValueError, match="unknown strategy"):
            await ix.reindex(strategy="nonsense")

        assert embedder.text_calls == []
        assert await ix.stored() == (None, None)
        assert await ix.chunk_count() == 0


class TestDefaultsAreUnchanged:
    """Every existing caller passes no strategy. What they get must be what they always got."""

    async def test_a_full_index_embeds_with_truncate(self, ix, embedder):
        await ix.full()

        assert embedder.strategies_used == {"truncate"}
        assert await ix.chunk_count() == 2

    async def test_an_incremental_index_embeds_with_truncate(self, ix, embedder, source):
        await ix.reindex()
        embedder.reset()

        await ix.reindex(commit_alpha_change(source))

        assert embedder.strategies_used == {"truncate"}
        assert len(embedder.embedded_texts) == 1

    async def test_a_current_index_is_left_alone(self, ix, embedder):
        await ix.reindex()
        embedder.reset()

        written = await ix.reindex()

        assert written == 0
        assert embedder.text_calls == []

    async def test_an_index_with_no_recorded_strategy_is_the_legacy_truncate_and_is_left_alone(self, ix, embedder):
        """NULL is what every index built before the column existed carries."""
        await ix.reindex()
        await ix.forget_the_strategy()
        embedder.reset()

        written = await ix.reindex()

        assert written == 0
        assert embedder.text_calls == []


class TestAStrategyChangeRebuildsTheWholeIndex:
    async def test_a_different_strategy_forces_a_full_reindex_even_when_the_commit_is_current(self, ix, embedder):
        await ix.reindex()
        embedder.reset()

        written = await ix.reindex(strategy="head_tail")

        assert written == 2
        assert embedder.strategies_used == {"head_tail"}
        assert sorted(embedder.embedded_texts) == sorted([ALPHA.rstrip("\n"), BETA.rstrip("\n")])

    async def test_the_rebuild_replaces_the_chunks_rather_than_adding_to_them(self, ix, embedder):
        await ix.reindex()

        await ix.reindex(strategy="head_tail")

        assert await ix.chunk_count() == 2

    async def test_a_different_strategy_beats_the_incremental_path_when_the_commit_has_also_moved(
        self, ix, embedder, source
    ):
        """An incremental pass here would re-embed alpha.py alone and leave
        beta.py's old vector next to it."""
        await ix.reindex()
        embedder.reset()
        new_sha = commit_alpha_change(source)

        written = await ix.reindex(new_sha, strategy="head_tail")

        assert written == 2
        assert any("beta" in text for text in embedder.embedded_texts)

    async def test_a_legacy_index_with_no_recorded_strategy_is_rebuilt_for_a_non_default_one(self, ix, embedder):
        await ix.reindex()
        await ix.forget_the_strategy()
        embedder.reset()

        written = await ix.reindex(strategy="windows")

        assert written == 2
        assert embedder.strategies_used == {"windows"}

    async def test_the_same_non_default_strategy_on_the_same_commit_does_nothing(self, ix, embedder):
        await ix.reindex(strategy="head_tail")
        embedder.reset()

        written = await ix.reindex(strategy="head_tail")

        assert written == 0
        assert embedder.text_calls == []

    async def test_a_failed_strategy_change_leaves_the_old_index_and_its_label_intact(
        self, ix, monkeypatch, embedder, source
    ):
        """The label and the vectors describe one index and move in one
        transaction. If the label survived a failed rebuild, the index would claim
        a strategy none of its vectors were built with."""
        await ix.reindex()
        rows_before = await ix.chunk_rows()

        class Failing(FakeEmbedder):
            def embed_texts(self, texts, strategy="truncate", **_):
                raise RuntimeError("embedder down")

        install_fake_embedder(monkeypatch, Failing())
        with pytest.raises(RuntimeError, match="embedder down"):
            await ix.reindex(strategy="head_tail")

        assert await ix.stored() == (source.sha, "truncate")
        assert await ix.chunk_rows() == rows_before


class TestTheStrategyIsRecordedWithTheCommit:
    async def test_a_full_index_records_the_strategy(self, ix, embedder, source):
        await ix.full(strategy="head_tail")

        assert await ix.stored() == (source.sha, "head_tail")

    async def test_a_default_index_records_truncate_explicitly(self, ix, embedder, source):
        await ix.reindex()

        assert await ix.stored() == (source.sha, "truncate")

    async def test_an_incremental_index_records_the_new_commit_with_the_same_strategy(self, ix, embedder, source):
        await ix.reindex(strategy="head_tail")
        new_sha = commit_alpha_change(source)

        await ix.reindex(new_sha, strategy="head_tail")

        assert await ix.stored() == (new_sha, "head_tail")

    async def test_a_strategy_change_records_the_new_strategy_on_the_unchanged_commit(self, ix, embedder, source):
        await ix.reindex()

        await ix.reindex(strategy="windows")

        assert await ix.stored() == (source.sha, "windows")
