import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
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
    QUEUED = "queued"
    RUNNING = "running"
    MERGED = "merged"
    CONFLICTING = "conflicting"
    FAILED = "failed"


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
    __table_args__ = (Index("ix_tasks_repo_id_created_at", "repo_id", text("created_at DESC")),)

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
