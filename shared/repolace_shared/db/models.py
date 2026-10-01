import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from repolace_shared.db.base import Base

CODE_CHUNK_EMBEDDING_DIM = 768


class TaskStatus(str, enum.Enum):
    """Where a task is in its lifecycle.

    ``PR_OPENED`` rather than ``merged``: a task's work ends when the PR
    exists. Whether it then merges is a property of the PR, decided by branch
    protection and human reviewers, which CLAUDE.md puts explicitly outside
    repolace's job. Nothing in the system subscribes to PR webhooks, so a
    ``merged`` state was one the pipeline could never actually set.
    """

    QUEUED = "queued"
    RUNNING = "running"
    PR_OPENED = "pr_opened"
    #: The pipeline ran to the end and opened no PR -- read `outcome` for why.
    #: Distinct from FAILED, which stays reserved for "repolace itself broke",
    #: so `error_message` keeps exactly one meaning.
    COMPLETED = "completed"
    CONFLICTING = "conflicting"
    FAILED = "failed"


class TaskOutcome(str, enum.Enum):
    """How a completed task scored against the success criteria.

    Separate from ``TaskStatus`` because they answer different questions.
    Status is about the pipeline ("did it get as far as opening a PR");
    outcome is about the benchmark ("was the issue actually fixed"). A task can
    reach ``PR_OPENED`` and still be ``FAILED`` -- that is precisely the case
    the benchmark exists to count.

    ``PASSED_WITH_TEST_EDIT`` is never folded into the headline figure. An
    agent that cannot pass a test can always claim the test is wrong, so the
    exception is tracked separately and gated on a human, not trusted.
    """

    PASSED = "passed"
    PASSED_WITH_TEST_EDIT = "passed_with_test_edit"
    FAILED = "failed"


#: The attempt number reserved for the pre-patch run. Every later run is an
#: edit attempt, numbered from 1.
BASELINE_ATTEMPT = 0

#: Why the agent loop ended, as stored in `tasks.agent_stop_reason`. A tuple of
#: strings behind a CHECK constraint rather than a Postgres enum, so adding a
#: value later is a drop-and-recreate of one constraint instead of an
#: `ALTER TYPE` (see migrations 0007 and 0009 for what the enum route costs).
#: `repolace_agents.contracts.StopReason` carries the same eight values, and a
#: test holds the two equal -- `agents` must not import this module, because it
#: would drag SQLAlchemy and pgvector into code that only needs to name a reason.
AGENT_STOP_REASONS: tuple[str, ...] = (
    "submitted",
    "step_cap",
    "budget_usd",
    "budget_calls",
    "budget_wall",
    "llm_error",
    "no_change",
    #: Every attempt was used and the last scored one still had visible
    #: regressions or collection errors. Distinct from `submitted` so a report
    #: can tell "submitted clean" from "ran out of attempts still red".
    "max_attempts",
)
_AGENT_STOP_REASON_LIST = ", ".join(f"'{reason}'" for reason in AGENT_STOP_REASONS)


class GithubInstallation(Base):
    __tablename__ = "github_installations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    account_login: Mapped[str] = mapped_column(String, nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_type: Mapped[str] = mapped_column(String, nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    repos: Mapped[list["RegisteredRepo"]] = relationship(back_populates="installation")


class RegisteredRepo(Base):
    __tablename__ = "registered_repos"
    __table_args__ = (Index("ix_registered_repos_installation_id", "installation_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    installation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_installations.id", ondelete="CASCADE"), nullable=False
    )
    github_repo_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    default_branch: Mapped[str] = mapped_column(String, nullable=False)
    private: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    indexed_commit_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The embedding strategy the index was built with. NULL means the legacy
    #: `truncate`, which is what every index built before this column used.
    #: Without it, changing the strategy on a repo whose index is already current
    #: never reindexes it -- `reindex_if_stale` compares commits only -- so old
    #: and new embeddings would mix in one index, and retrieval quality would be
    #: a silent blend of two experiments.
    index_strategy: Mapped[str | None] = mapped_column(String, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    installation: Mapped[GithubInstallation] = relationship(back_populates="repos")
    tasks: Mapped[list["Task"]] = relationship(back_populates="repo")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_repo_id_created_at", "repo_id", text("created_at DESC")),
        # The escape hatch has to be structurally impossible to use silently.
        # An unexplained `passed_with_test_edit` would be indistinguishable
        # from a pass, which is exactly the loophole that would hollow out the
        # benchmark number this project's claim rests on.
        CheckConstraint(
            "outcome IS DISTINCT FROM 'passed_with_test_edit' OR test_edit_justification IS NOT NULL",
            name="ck_tasks_test_edit_needs_justification",
        ),
        CheckConstraint(
            "(test_edit_approved_by IS NULL) = (test_edit_approved_at IS NULL)",
            name="ck_tasks_test_edit_approval_is_complete",
        ),
        # Sign-off on anything else would be meaningless, and would suggest the
        # approval was recorded against the wrong task.
        CheckConstraint(
            "test_edit_approved_at IS NULL OR outcome = 'passed_with_test_edit'",
            name="ck_tasks_test_edit_approval_needs_test_edit",
        ),
        # Built from `AGENT_STOP_REASONS` so the constraint and the constant
        # cannot drift. A string plus a CHECK, not an enum: see the constant.
        CheckConstraint(
            f"agent_stop_reason IS NULL OR agent_stop_reason IN ({_AGENT_STOP_REASON_LIST})",
            name="ck_tasks_agent_stop_reason",
        ),
        # The three eval columns are all NULL (a product task) or all set (a
        # benchmark row). The unique index below only compares rows where all
        # three are non-NULL -- NULLs are distinct in a Postgres unique index --
        # so without this an enqueue that forgot `run_index` would silently lose
        # the idempotency the index exists for.
        CheckConstraint(
            "(eval_run_id IS NULL) = (instance_id IS NULL) AND (eval_run_id IS NULL) = (run_index IS NULL)",
            name="ck_tasks_eval_columns_together",
        ),
        # Makes the harness's enqueue idempotent: a re-run after a crash cannot
        # create a second row for one (run, instance, run_index), which would be
        # scored twice. Partial because every product task has a NULL
        # `eval_run_id`, and those must stay free to repeat.
        Index(
            "uq_tasks_eval_instance_run",
            "eval_run_id",
            "instance_id",
            "run_index",
            unique=True,
            postgresql_where=text("eval_run_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("registered_repos.id", ondelete="CASCADE"), nullable=False
    )
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    issue_title: Mapped[str] = mapped_column(String, nullable=False)
    issue_url: Mapped[str] = mapped_column(String, nullable=False)
    #: UNTRUSTED. Anyone can file an issue on a public repo, so this reaches an
    #: LLM prompt only as delimited data, never as instruction. NULL for tasks
    #: created before the column existed, which is not the same as an empty body.
    issue_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_branch: Mapped[str] = mapped_column(String, nullable=False)
    #: Set only by the eval harness, so benchmark rows are queryable from this
    #: table rather than from a second store that could drift from it.
    eval_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    instance_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            name="task_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=TaskStatus.QUEUED,
    )
    #: Null until the task finishes. A task still running has no outcome yet,
    #: which is a different thing from having failed.
    outcome: Mapped["TaskOutcome | None"] = mapped_column(
        Enum(
            TaskOutcome,
            name="task_outcome",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=True,
    )
    #: Why the agent believed editing a test was part of fixing the issue.
    #: Required by a check constraint whenever the outcome is a test edit.
    test_edit_justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Human sign-off. Until this is set, a `passed_with_test_edit` outcome is
    #: recorded but does not count towards anything.
    test_edit_approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    test_edit_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Whether to open a PR even when verification failed. False by default, so
    #: a repolace PR means the tests passed unless someone opted out.
    open_pr_on_failure: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    #: What the agent actually produced. The clone is deleted when the task
    #: ends, so without these a PASSED verdict cannot be re-examined afterwards
    #: -- and a benchmark nobody can audit is a benchmark nobody should believe.
    #: NULL means the task never got this far, which is not the same as a patch
    #: that changed nothing.
    patch_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The final diff itself (`review_diff()`), for every task that got as far as
    #: the agent. `patch_sha` is a git sha and cannot carry the content: a
    #: benchmark task that does not score PASSED never pushes, so its commits die
    #: with the clone and the sha points at nothing. NULL means the task never
    #: reached the agent; an empty string would be a patch that changed nothing.
    patch_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_files: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: `Score.reason`, which `score()` computes and the pipeline used to discard.
    #: The report needs the sentence, and rescoring later is lossy: the set that
    #: spares a shipped module like `django/test/client.py` (`baseline_files`) is
    #: not stored. NULL for a task that was never scored.
    score_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Why the agent loop ended -- one of `AGENT_STOP_REASONS`. Its own column
    #: because `error_message` is reserved for FAILED ("repolace itself broke"),
    #: and an agent that ran out of budget did not break repolace. NULL for a
    #: task that never ran an agent.
    agent_stop_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    repo: Mapped[RegisteredRepo] = relationship(back_populates="tasks")
    test_runs: Mapped[list["TaskTestRun"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskTestRun.attempt"
    )
    #: `passive_deletes` so deleting a task leans on the database's ON DELETE
    #: CASCADE instead of loading every call -- each row carries a full prompt.
    llm_calls: Mapped[list["LLMCall"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="LLMCall.created_at",
    )


class TaskTestRun(Base):
    """One execution of the repo's test suite during a task.

    A row per attempt rather than two columns on ``Task``, because the pipeline
    loops: baseline, then a run after each bounded-retry edit. Keeping every
    run is what lets the trace UI show an attempt getting closer, and what lets
    the benchmark be rescored later without re-running anything.

    Stores the raw pass/fail sets rather than the derived fail-to-pass and
    pass-to-pass lists. The scoring rule is young and still has open questions
    (whether the agent may add its own tests, whether diff size is capped), so
    the sets it is computed *from* are the durable thing to keep.
    """

    __tablename__ = "task_test_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt", name="uq_task_test_runs_task_id_attempt"),
        CheckConstraint("attempt >= 0", name="ck_task_test_runs_attempt_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    #: 0 is the baseline run at the base commit; 1..N are the edit attempts.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Which commit was actually tested. Without it a run cannot be tied back
    #: to the diff that produced it once the clone is gone.
    commit_sha: Mapped[str] = mapped_column(String, nullable=False)
    passed: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    failed: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    #: In neither set, and kept because the scoring rule asks about them: a test
    #: that passed at baseline and is skipped afterwards is a regression, and a
    #: baseline failure that is silenced rather than fixed is a disqualification.
    #: Deriving either later is impossible if the sets were discarded.
    skipped: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    #: Expected failures, separated from `skipped` because pytest reports both
    #: the same way and the scoring rule treats them oppositely: an xfail is red
    #: at baseline and counts toward fail-to-pass, an ordinary skip is not and
    #: does not. Stored rather than derived for the reason 0009 gives for the
    #: sets it added -- a dropped set cannot be recovered, and "was this an xfail
    #: or a skip" is exactly the question the next revision of the rule asks.
    xfailed: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    did_not_run: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    collect_failures: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    #: The files pytest actually collected tests from, and the conftests it
    #: loaded. Authoritative for this repository in a way the path heuristic
    #: cannot be, and read by `disqualifying_paths` -- so a run stored without
    #: it cannot be rescored, only re-run.
    collected_files: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    conftests: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    #: rootdir, the watched ini options and the registered plugins. `score`
    #: refuses to compare two runs whose fingerprints differ, because node ids
    #: are relative to rootdir and an ini option decides whether a warning is an
    #: error -- so an agent could turn a real failure into a real pass without
    #: touching a test file. Kept so that refusal is reproducible after the fact.
    fingerprint: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    #: The tail of what the container printed. The only human-readable account
    #: of an unscoreable run, and the first thing anyone asks for.
    stdout_tail: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)
    #: Set when the suite produced no usable result at all -- an import error,
    #: a missing dependency, a timeout. Distinguishes "nothing passed" from
    #: "we never found out", and a baseline that never ran makes the whole task
    #: unscoreable rather than a failure.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[Task] = relationship(back_populates="test_runs")


class LLMCall(Base):
    """One call through the gateway.

    Stores the request and response verbatim, for the reason `TaskTestRun`
    stores raw sets: cost, latency and the trajectory itself are the benchmark's
    evidence, and none of them can be recovered once the call is over. The price
    is that this table holds repo source code, which is a privacy matter for
    private repositories.

    Token and cost columns are NULL on a call that produced no usage -- an
    error row -- rather than 0, because "the provider reported nothing" and
    "the call was free" are different claims and only the first is true there.

    ``input_tokens`` is the *total* prompt, cached tokens included (LiteLLM's and
    OpenAI's convention); ``cached_input_tokens`` is the cache-read part of it.
    Cache-*write* tokens, which Anthropic bills at a premium, are not a column:
    they are in ``response.usage`` for anyone repricing a run.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("ix_llm_calls_task_id_created_at", "task_id", "created_at"),
        CheckConstraint("attempt IS NULL OR attempt >= 0", name="ck_llm_calls_attempt_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    #: The edit attempt the call belonged to. NULL for a call outside any
    #: attempt (retrieval-time query rewriting, say), which is not attempt 0:
    #: 0 is the baseline test run and no model call ever belongs to it.
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    #: The LiteLLM model id actually called, provider prefix included. The model
    #: that *served* the call, so a fallback shows up as the fallback.
    model: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[Task] = relationship(back_populates="llm_calls")


class ChunkType(str, enum.Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS_SKELETON = "class_skeleton"
    MODULE = "module"


class CodeChunk(Base):
    __tablename__ = "code_chunks"
    __table_args__ = (
        Index("ix_code_chunks_repo_id_commit_sha", "repo_id", "commit_sha"),
        Index("ix_code_chunks_repo_id_file_path", "repo_id", "file_path"),
        # Both retrieval arms. Migration 0004 creates these, but until they
        # were declared here the model and the database disagreed, and
        # `alembic revision --autogenerate` emitted a `drop_index` for each --
        # a migration that would quietly delete the indexes hybrid search
        # depends on, with nothing failing to show for it.
        Index("ix_code_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
        Index(
            "ix_code_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("registered_repos.id", ondelete="CASCADE"), nullable=False
    )
    # The commit at which THIS ROW was last written -- not an index-version
    # marker. After an incremental reindex, rows for unchanged files keep an
    # older sha while registered_repos.indexed_commit_sha advances, so no single
    # value here selects the complete index. Filter on repo_id; to ask "what is
    # this repo indexed at", read RegisteredRepo.indexed_commit_sha.
    commit_sha: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_type: Mapped[ChunkType] = mapped_column(
        Enum(
            ChunkType,
            name="chunk_type",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    class_name: Mapped[str | None] = mapped_column(String, nullable=True)
    symbol_name: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(CODE_CHUNK_EMBEDDING_DIM), nullable=False)
    content_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    repo: Mapped[RegisteredRepo] = relationship()
