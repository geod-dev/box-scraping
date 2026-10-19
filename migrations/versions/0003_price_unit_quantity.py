"""Add scrape_observations.price_unit_quantity (price expressed per N units, e.g. per 1000 sheets).

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LATEST_VIEW = """
    CREATE VIEW latest_price_observations AS
    SELECT o.*
    FROM scrape_observations o
    JOIN (
        SELECT DISTINCT ON (product_id) product_id, capture_id
        FROM scrape_observations
        ORDER BY product_id, observed_at DESC, id DESC
    ) latest ON latest.product_id = o.product_id AND latest.capture_id = o.capture_id
"""
PRICE_FUNCTION = """
    CREATE FUNCTION price_for_quantity(p_product_id bigint, p_quantity numeric)
    RETURNS SETOF scrape_observations
    LANGUAGE sql STABLE AS $$
        SELECT o.*
        FROM latest_price_observations o
        WHERE o.product_id = p_product_id
          AND (o.quantity IS NULL OR o.quantity = p_quantity)
          AND (o.min_quantity IS NULL OR o.min_quantity <= p_quantity)
          AND (o.max_quantity IS NULL
               OR p_quantity < o.max_quantity
               OR (o.max_inclusive AND p_quantity = o.max_quantity))
        ORDER BY o.min_quantity DESC NULLS LAST, o.id
    $$
"""


def _recreate_dependants(change) -> None:
    # `SELECT o.*` in the view is expanded at creation time: recreate it to include the column.
    op.execute("DROP FUNCTION IF EXISTS price_for_quantity(bigint, numeric)")
    op.execute("DROP VIEW IF EXISTS latest_price_observations")
    change()
    op.execute(LATEST_VIEW)
    op.execute(PRICE_FUNCTION)


def upgrade() -> None:
    _recreate_dependants(
        lambda: op.add_column(
            "scrape_observations",
            sa.Column(
                "price_unit_quantity",
                sa.Numeric(),
                nullable=True,
                comment="The price applies to this many price_unit (e.g. 1000 for '/ 1 000 feuilles'); NULL = unknown",
            ),
        )
    )
    # Rows written before this column existed: their unit text said "/ 1 <unit>" unless stated otherwise.
    op.execute(
        """
        UPDATE scrape_observations SET price_unit_quantity = 1
        WHERE price_basis = 'unit' AND coalesce(metadata->>'unit_text', '') ~ '^/ ?1 [^0-9]'
        """
    )


def downgrade() -> None:
    _recreate_dependants(lambda: op.drop_column("scrape_observations", "price_unit_quantity"))
