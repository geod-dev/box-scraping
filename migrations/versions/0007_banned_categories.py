"""Banned categories: a banned category is deleted with everything below it and skipped by
the crawls; its own metadata is kept in ``banned_categories`` to recreate it on unban.

Backward compatible with the previous release during a deploy (new table only).

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "banned_categories",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("competitor_id", sa.Integer(), nullable=False),
        sa.Column(
            "category_id",
            sa.BigInteger(),
            nullable=False,
            comment="id of the deleted category (plain id: the row no longer exists)",
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False, comment="canonical URL: crawls skip it and every page below it"),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=True),
        sa.Column("breadcrumb", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("parent_url", sa.Text(), nullable=True, comment="URL of the parent, to link it again on unban"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("banned_by", sa.Text(), nullable=True),
        sa.Column("banned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "deleted",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="what the ban deleted (counts per kind)",
        ),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("competitor_id", "url", name="uq_banned_categories_competitor_url"),
        comment="Categories excluded from scraping; their own metadata is kept to recreate them on unban",
    )
    op.create_index(
        "ix_banned_categories_competitor_external_id", "banned_categories", ["competitor_id", "external_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_banned_categories_competitor_external_id", table_name="banned_categories")
    op.drop_table("banned_categories")
