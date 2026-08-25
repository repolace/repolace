"""Unit tests for keyword-arm query construction.

No database connection: the tsquery expression is compiled against the
PostgreSQL dialect and asserted as SQL text.

Note this module transitively imports `retrieval.embed`, which imports
sentence-transformers (~8s, pulls torch). That is itself worth fixing — query
construction should not require a deep-learning stack to import.
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from retrieval.retrieve import _or_tsquery


@dataclass(frozen=True)
class Compiled:
    sql: str
    params: dict

    @property
    def call_count(self) -> int:
        return self.sql.count("plainto_tsquery(")

    @property
    def values(self) -> list:
        return list(self.params.values())


def compiled(query: str) -> Compiled:
    """Compile the tsquery expression against the PostgreSQL dialect.

    Bound params are kept rather than inlined: the `'simple'` argument binds as
    REGCONFIG, which has no literal renderer.
    """
    stmt = select(_or_tsquery(query))
    c = stmt.compile(dialect=postgresql.dialect())
    return Compiled(sql=str(c), params=c.params)


class TestOrTsquery:
    def test_single_word_produces_one_plainto_tsquery(self):
        result = compiled("retry")

        assert result.call_count == 1
        assert "retry" in result.values

    def test_multiple_words_are_or_combined(self):
        result = compiled("retry backoff timeout")

        assert result.call_count == 3
        assert "||" in result.sql
        assert {"retry", "backoff", "timeout"} <= set(result.values)

    def test_uses_the_simple_config_so_code_is_not_stemmed(self):
        result = compiled("retrying handlers")

        assert "simple" in result.values
        assert "english" not in result.values

    def test_whitespace_is_collapsed(self):
        assert compiled("retry   backoff").call_count == 2

    def test_empty_query_falls_back_without_raising(self):
        result = compiled("")

        assert result.call_count == 1
        assert "||" not in result.sql

    def test_whitespace_only_query_falls_back_without_raising(self):
        result = compiled("   \t  ")

        assert result.call_count == 1
        assert "||" not in result.sql


class TestKnownGaps:
    @pytest.mark.xfail(strict=True, reason="H2: sub-lexemes inside an identifier are still ANDed")
    def test_snake_case_identifiers_are_expanded_to_or_semantics(self):
        """`_or_tsquery` claims to avoid plainto_tsquery's ANDing, but only does
        so *between* whitespace-separated words.

        Verified against PostgreSQL 16:
            plainto_tsquery('simple','get_user_id')  ->  'get' & 'user' & 'id'

        The default parser splits on underscores, so every identifier-shaped
        term — the ones that matter most for code search — still demands that
        all of its sub-lexemes appear in one chunk. Fixing it means expanding
        each word's lexemes (e.g. via `tsvector_to_array(to_tsvector(...))`)
        and OR-ing those, which this test detects.
        """
        result = compiled("get_user_id")

        assert "tsvector_to_array" in result.sql or "unnest" in result.sql

    @pytest.mark.xfail(strict=True, reason="no cap on term count")
    def test_long_queries_are_capped(self):
        """A pasted issue body should not become an N-term OR chain: as an OR it
        destroys `@@` selectivity and forces ts_rank over most of the repo.
        """
        assert compiled(" ".join(f"word{i}" for i in range(500))).call_count <= 50

    @pytest.mark.xfail(strict=True, reason="no term dedupe")
    def test_repeated_terms_are_deduped(self):
        assert compiled("retry retry retry backoff").call_count == 2
