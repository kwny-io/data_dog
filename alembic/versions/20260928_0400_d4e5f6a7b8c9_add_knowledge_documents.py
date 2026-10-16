"""add knowledge_documents and knowledge_chunks (A7 用户文档导入)

用户自带厂商文档的落库表：`knowledge_documents`（文档元数据）+ `knowledge_chunks`
（切片文本）。两张表都**租户作用域**（tenant_id 进主键）——用户导入的是私有
厂商原件，不能被别的租户检索到。

- `knowledge_documents`：主键 (tenant_id, document_id)；document_id 内容寻址。
- `knowledge_chunks`：主键 (tenant_id, chunk_id)，document_id 建索引（按文档删/查）。

可逆：downgrade 直接删两表（纯新增，无既有数据受影响）。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'd4e5f6a7b8c9'
down_revision: str | None = 'c3d4e5f6a7b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _s(n: int) -> sa.String:
    return sa.String(length=n)


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("tenant_id", _s(80), nullable=False),
        sa.Column("document_id", _s(80), nullable=False),
        sa.Column("source_name", _s(200), nullable=False),
        sa.Column("content_sha256", _s(64), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "document_id",
                                name="pk_knowledge_documents"),
    )
    with op.batch_alter_table("knowledge_documents", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_knowledge_documents_tenant_id"),
                              ["tenant_id"], unique=False)

    op.create_table(
        "knowledge_chunks",
        sa.Column("tenant_id", _s(80), nullable=False),
        sa.Column("chunk_id", _s(80), nullable=False),
        sa.Column("document_id", _s(80), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "chunk_id",
                                name="pk_knowledge_chunks"),
    )
    with op.batch_alter_table("knowledge_chunks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_knowledge_chunks_tenant_id"),
                              ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_knowledge_chunks_document_id"),
                              ["document_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("knowledge_chunks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_knowledge_chunks_document_id"))
        batch_op.drop_index(batch_op.f("ix_knowledge_chunks_tenant_id"))
    op.drop_table("knowledge_chunks")

    with op.batch_alter_table("knowledge_documents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_knowledge_documents_tenant_id"))
    op.drop_table("knowledge_documents")
