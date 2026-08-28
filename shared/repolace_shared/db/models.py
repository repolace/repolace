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
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
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
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("registered_repos.id", ondelete="CASCADE"), nullable=False
    )
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    issue_title: Mapped[str] = mapped_column(String, nullable=False)
    issue_url: Mapped[str] = mapped_column(String, nullable=False)
    target_branch: Mapped[str] = mapped_column(String, nullable=False)
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
    changed_files: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    did_not_run: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    collect_failures: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)
    #: Set when the suite produced no usable result at all -- an import error,
    #: a missing dependency, a timeout. Distinguishes "nothing passed" from
    #: "we never found out", and a baseline that never ran makes the whole task
    #: unscoreable rather than a failure.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[Task] = relationship(back_populates="test_runs")


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
