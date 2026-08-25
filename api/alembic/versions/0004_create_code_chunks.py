"""enable pgvector and create code_chunks

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-25

"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

chunk_type = postgresql.ENUM("function", "method", "class_skeleton", name="chunk_type", create_type=False)

EMBEDDING_DIM = 768


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    chunk_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "code_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("commit_sha", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("chunk_type", chunk_type, nullable=False),
        sa.Column("class_name", sa.String(), nullable=True),
        sa.Column("symbol_name", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(EMBEDDING_DIM), nullable=False),
        sa.Column(
            "content_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repo_id"], ["registered_repos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index("ix_code_chunks_repo_id_commit_sha", "code_chunks", ["repo_id", "commit_sha"])
    op.create_index("ix_code_chunks_repo_id_file_path", "code_chunks", ["repo_id", "file_path"])
    op.create_index("ix_code_chunks_content_tsv", "code_chunks", ["content_tsv"], postgresql_using="gin")
    op.execute(
        "CREATE INDEX ix_code_chunks_embedding ON code_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index("ix_code_chunks_embedding", table_name="code_chunks")
    op.drop_index("ix_code_chunks_content_tsv", table_name="code_chunks")
    op.drop_index("ix_code_chunks_repo_id_file_path", table_name="code_chunks")
    op.drop_index("ix_code_chunks_repo_id_commit_sha", table_name="code_chunks")
    op.drop_table("code_chunks")
    chunk_type.drop(op.get_bind())
    op.execute("DROP EXTENSION IF EXISTS vector")
