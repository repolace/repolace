import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from repolace_shared.db.base import Base


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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    installation: Mapped[GithubInstallation] = relationship(back_populates="repos")
