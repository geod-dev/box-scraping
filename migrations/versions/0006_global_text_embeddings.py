"""Global embedding cache: ``text_embeddings`` holds one vector per embedded text, shared by
every product with that text. The vectors already paid for in ``product_embeddings`` are
copied there (same text hash), so nothing is embedded again.

Backward compatible with the previous release during a deploy: ``product_embeddings.embedding``
is only made nullable (no longer written) and dropped by a later migration.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "text_embeddings",
        sa.Column("text_hash", sa.Text(), nullable=False, comment="sha256 of model, dimensions, input type and text"),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True, comment="the embedded text (null for rows migrated from 0005)"),
        sa.Column("tokens", sa.Integer(), nullable=True, comment="tokens billed for this text (estimated share)"),
        sa.Column("embedding", sa.LargeBinary(), nullable=False, comment="float32 little-endian, L2-normalized"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("text_hash"),
        comment="Global embedding cache: one row per embedded text (model and dimensions in the hash)",
    )
    op.execute(
        """
        INSERT INTO text_embeddings (text_hash, model, dimensions, tokens, embedding, created_at, updated_at)
        SELECT DISTINCT ON (text_hash) text_hash, model, dimensions, tokens, embedding, created_at, updated_at
        FROM product_embeddings
        WHERE embedding IS NOT NULL
        ORDER BY text_hash, tokens DESC NULLS LAST, updated_at DESC
        """
    )
    op.alter_column(
        "product_embeddings",
        "embedding",
        nullable=True,
        comment="deprecated: vectors are in text_embeddings (dropped by a later migration)",
    )
    op.alter_column(
        "product_embeddings",
        "tokens",
        comment="tokens billed when this product's text was embedded (null = reused)",
    )
    op.execute("COMMENT ON TABLE product_embeddings IS 'Text embedded for each product (vector in text_embeddings)'")


def downgrade() -> None:
    op.execute(
        """
        UPDATE product_embeddings p SET embedding = t.embedding
        FROM text_embeddings t
        WHERE t.text_hash = p.text_hash AND p.embedding IS NULL
        """
    )
    op.execute("DELETE FROM product_embeddings WHERE embedding IS NULL")
    op.alter_column("product_embeddings", "embedding", nullable=False, comment="float32 little-endian, L2-normalized")
    op.alter_column("product_embeddings", "tokens", comment="tokens billed for this text (null = shared/unknown)")
    op.execute(
        "COMMENT ON TABLE product_embeddings IS "
        "'Embedding of each product''s matching text (title + description), reused while unchanged'"
    )
    op.drop_table("text_embeddings")
