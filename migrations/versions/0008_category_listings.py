"""Extra categories: ``category_listings`` records the families/products a category lists on the
site although they belong to another category, and ``categories.role`` tells main categories
(their subtree owns products) from extra ones (they only list products of other categories).

Backward compatible with the previous release during a deploy (new table, new column with a
default). Every category starts as ``main``: the links, and so the extra categories, come with
the next catalog sync.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "categories",
        sa.Column(
            "role",
            sa.Text(),
            server_default=sa.text("'main'"),
            nullable=False,
            comment="extra: its subtree owns no product but lists products of other categories; main otherwise",
        ),
    )
    op.create_check_constraint("ck_categories_role", "categories", "role IN ('main', 'extra')")
    op.create_table(
        "category_listings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("competitor_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False, comment="the category that lists"),
        sa.Column(
            "listed_category_id", sa.BigInteger(), nullable=True, comment="a listed category (family), with its subtree"
        ),
        sa.Column("listed_product_id", sa.BigInteger(), nullable=True, comment="a listed product"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_run_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "(listed_category_id IS NULL) <> (listed_product_id IS NULL)", name="ck_category_listings_one_target"
        ),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["listed_category_id"], ["categories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["listed_product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["last_seen_run_id"], ["scrape_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        comment="Families/products a category lists on the site although they belong to another category",
    )
    op.create_index(
        "uq_category_listings_category",
        "category_listings",
        ["category_id", "listed_category_id"],
        unique=True,
        postgresql_where=sa.text("listed_category_id IS NOT NULL"),
    )
    op.create_index(
        "uq_category_listings_product",
        "category_listings",
        ["category_id", "listed_product_id"],
        unique=True,
        postgresql_where=sa.text("listed_product_id IS NOT NULL"),
    )
    op.create_index("ix_category_listings_competitor", "category_listings", ["competitor_id"])
    op.create_index("ix_category_listings_listed_category", "category_listings", ["listed_category_id"])
    op.create_index("ix_category_listings_listed_product", "category_listings", ["listed_product_id"])


def downgrade() -> None:
    op.drop_table("category_listings")
    op.drop_constraint("ck_categories_role", "categories", type_="check")
    op.drop_column("categories", "role")
