"""Unbounded NUMERIC for prices and quantities (no silent rounding).

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
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
COLUMNS = {"price": (14, 4), "quantity": (18, 4), "min_quantity": (18, 4), "max_quantity": (18, 4)}


def _retype(numeric_for) -> None:
    # The view/function depend on the table row type: recreate them around the change.
    op.execute("DROP FUNCTION IF EXISTS price_for_quantity(bigint, numeric)")
    op.execute("DROP VIEW IF EXISTS latest_price_observations")
    for column, precision in COLUMNS.items():
        op.alter_column("scrape_observations", column, type_=numeric_for(precision))
    op.execute(LATEST_VIEW)
    op.execute(PRICE_FUNCTION)


def upgrade() -> None:
    _retype(lambda _precision: sa.Numeric())


def downgrade() -> None:
    _retype(lambda precision: sa.Numeric(*precision))
