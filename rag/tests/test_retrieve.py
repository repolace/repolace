"""Unit tests for keyword-arm query construction.

No database connection: the tsquery expression is compiled against the
PostgreSQL dialect and asserted as SQL text.

Note this module transitively imports `retrieval.embed`, which imports
sentence-transformers (~8s, pulls torch). That is itself worth fixing — query
construction should not require a deep-learning stack to import.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from retrieval.config import MAX_QUERY_TERMS
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


class TestIdentifierSplitting:
    """Identifiers must contribute OR-able sub-terms, not one conjunction.

    Verified against PostgreSQL 16:
        plainto_tsquery('simple','get_user_id')  ->  'get' & 'user' & 'id'

    The default parser splits identifiers on non-alphanumerics, so passing a
    whole identifier to plainto_tsquery demands that every sub-lexeme appear in
    the same chunk. Splitting the query the same way the parser splits the
    content is what restores partial-overlap matching.
    """

    def test_snake_case_identifier_becomes_separate_or_ed_terms(self):
        result = compiled("get_user_id")

        assert result.call_count == 3
        assert "||" in result.sql
        assert {"get", "user", "id"} <= set(result.values)

    def test_dotted_attribute_access_is_split(self):
        result = compiled("db.session.commit")

        assert {"db", "session", "commit"} <= set(result.values)

    def test_camel_case_is_left_intact(self):
        """Postgres lowercases but does not split camelCase, so neither do we."""
        result = compiled("camelCaseName")

        assert result.call_count == 1
        assert "camelCaseName" in result.values

    def test_punctuation_only_query_falls_back_without_raising(self):
        result = compiled("!!! ???")

        assert result.call_count == 1
        assert "||" not in result.sql


class TestQueryTermBudget:
    def test_long_queries_are_capped(self):
        """A pasted issue body must not become an N-term OR chain: as an OR it
        destroys `@@` selectivity and forces ts_rank over most of the repo.
        """
        assert compiled(" ".join(f"word{i}" for i in range(500))).call_count == MAX_QUERY_TERMS

    def test_repeated_terms_are_deduped(self):
        assert compiled("retry retry retry backoff").call_count == 2

    def test_dedupe_is_case_insensitive(self):
        assert compiled("Retry retry RETRY").call_count == 1
