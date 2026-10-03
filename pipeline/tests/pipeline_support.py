"""Builders for pipeline test data.

Plain constructors rather than fixtures: the stub editor takes plain data by
design, so building it should not need pytest machinery.
"""

import dataclasses
import uuid
from decimal import Decimal

from verify.protocol import SuiteResult
from verify.scoring import Score, Verdict

from repolace_pipeline.context import RetrievedChunk
from repolace_pipeline.edit import StubEditRequest
from repolace_pipeline.pr import PrFacts

TASK_ID = uuid.UUID("2f8a1c4e-0000-4000-8000-000000000001")


def chunk(
    file_path: str = "src/app.py",
    start_line: int = 3,
    end_line: int = 9,
    chunk_type: str = "function",
    symbol_name: str = "parse_config",
    class_name: str | None = None,
    rrf_score: float = 0.0328,
    semantic_rank: int | None = 1,
    keyword_rank: int | None = 3,
) -> RetrievedChunk:
    return RetrievedChunk(
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        chunk_type=chunk_type,
        symbol_name=symbol_name,
        class_name=class_name,
        rrf_score=rrf_score,
        semantic_rank=semantic_rank,
        keyword_rank=keyword_rank,
    )


def request(*chunks: RetrievedChunk, indexed_chunk_count: int = 238) -> StubEditRequest:
    return StubEditRequest(
        task_id=TASK_ID,
        issue_number=7,
        issue_title="parse_config crashes on an empty file",
        issue_url="https://github.com/acme/sample/issues/7",
        target_branch="main",
        base_sha="a1b2c3d4e5f6a7b8c9d0",
        indexed_chunk_count=indexed_chunk_count,
        retrieved=chunks or (chunk(),),
    )


def empty_request() -> StubEditRequest:
    """A request whose retrieval came back empty -- the case the editor must refuse."""
    return dataclasses.replace(request(), retrieved=())


def pr_facts(**overrides) -> PrFacts:
    """A product-mode `PrFacts` for a clean, submitted change. Override what a test is about."""
    fields = dict(
        task_id=TASK_ID,
        issue_number=7,
        issue_title="parse_config crashes on an empty file",
        instance_id=None,
        plumbing_only=False,
        summary="Return an empty dict when the file is empty.",
        changed_files=("src/app.py",),
        baseline=SuiteResult(passed=("t::a", "t::b"), failed=(), skipped=("t::s",)),
        final=SuiteResult(passed=("t::a", "t::b", "t::c"), failed=(), skipped=("t::s",)),
        verdict=Verdict(ok=True, reason="no regression, silenced failure or new collection error found"),
        scored=Score(outcome=None, reason="inadmissible", inadmissible=True),
        expected_fail_to_pass=None,
        attempts=2,
        stop_reason="submitted",
        model="claude-sonnet-5-5",
        cost_usd=Decimal("0.4213"),
        retrieved=(chunk(),),
    )
    return PrFacts(**{**fields, **overrides})


def benchmark_facts(**overrides) -> PrFacts:
    """A benchmark-mode `PrFacts`: `instance_id` is the one switch, the rest follows from it."""
    fields = dict(
        instance_id="psf__requests-2317",
        expected_fail_to_pass=("tests/test_hidden.py::test_f2p",),
        final=SuiteResult(passed=("t::a", "tests/test_hidden.py::test_f2p")),
        baseline=SuiteResult(passed=("t::a",), failed=("tests/test_hidden.py::test_f2p",)),
        scored=Score(
            outcome=None,
            reason="1 fail-to-pass, no regressions",
            fail_to_pass=("tests/test_hidden.py::test_f2p",),
        ),
    )
    return pr_facts(**{**fields, **overrides})
