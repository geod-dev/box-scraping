"""Product matching against the reference catalog (Antalis): embeddings cache, matches,
candidates, history; ``matching`` worker task; listing page cache (resumable catalog syncs);
jobs remember the job they restart (``worker_jobs.resumed_from_job_id``). Replaces the generic
pairwise ``product_matches`` table (never used: no data to migrate).

Backward compatible with the previous release, which keeps running during a deploy (the old
worker finishes its current page): ``products.antalis_reference`` is no longer used by the
code but is only dropped by a later migration, once no old worker can write it.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB_EMPTY_OBJECT = sa.text("'{}'::jsonb")
JSONB_EMPTY_ARRAY = sa.text("'[]'::jsonb")
STATUSES = "('auto_matched', 'needs_review', 'no_match', 'validated', 'rejected', 'deprecated')"
EVENTS = "('computed', 'recomputed', 'validated', 'rejected', 'deprecated', 'reset')"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.drop_table("product_matches")

    op.drop_constraint("ck_worker_jobs_task", "worker_jobs", type_="check")
    op.create_check_constraint(
        "ck_worker_jobs_task", "worker_jobs", "task IN ('catalog_sync', 'price_refresh', 'matching')"
    )
    op.add_column(
        "worker_jobs",
        sa.Column(
            "resumed_from_job_id",
            sa.BigInteger(),
            nullable=True,
            comment="The interrupted/stopped job this one restarts (manual or automatic resume)",
        ),
    )
    op.create_foreign_key(
        "fk_worker_jobs_resumed_from",
        "worker_jobs",
        "worker_jobs",
        ["resumed_from_job_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "product_embeddings",
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("text_hash", sa.Text(), nullable=False, comment="sha256 of model, dimensions, input type and text"),
        sa.Column("tokens", sa.Integer(), nullable=True, comment="tokens billed for this text (null = shared/unknown)"),
        sa.Column("embedding", sa.LargeBinary(), nullable=False, comment="float32 little-endian, L2-normalized"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("product_id"),
        comment="Embedding of each product's matching text (title + description), reused while unchanged",
    )

    op.create_table(
        "product_matches",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("competitor_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "antalis_product_id",
            sa.BigInteger(),
            nullable=True,
            comment="The associated reference product (auto_matched, validated); null otherwise",
        ),
        sa.Column(
            "proposed_antalis_product_id",
            sa.BigInteger(),
            nullable=True,
            comment="Best candidate found by the engine",
        ),
        sa.Column(
            "score",
            sa.Numeric(5, 2),
            nullable=True,
            comment="Similarity score 0-100 of the proposed candidate (not a probability)",
        ),
        sa.Column("runner_up_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("score_gap", sa.Numeric(5, 2), nullable=True),
        sa.Column("dimensions_compared", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("decision_reasons", postgresql.JSONB(), server_default=JSONB_EMPTY_ARRAY, nullable=False),
        sa.Column("product_hash", sa.Text(), nullable=True, comment="Hash of the competitor product's matching fields"),
        sa.Column(
            "antalis_product_hash",
            sa.Text(),
            nullable=True,
            comment="Hash of the associated (else proposed) reference product's matching fields",
        ),
        sa.Column("reference_catalog_hash", sa.Text(), nullable=True, comment="Reference catalog state when computed"),
        sa.Column("config_hash", sa.Text(), nullable=True, comment="Matching configuration when computed"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column(
            "manual_choice",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="validated with a product other than the engine's proposal",
        ),
        sa.Column(
            "rejected_antalis_product_ids",
            postgresql.JSONB(),
            server_default=JSONB_EMPTY_ARRAY,
            nullable=False,
            comment="Reference products a human rejected for this product",
        ),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deprecated_reason", sa.Text(), nullable=True),
        sa.Column("previous_status", sa.Text(), nullable=True, comment="Status before deprecation"),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False, comment="Optimistic lock"),
        *_timestamps(),
        sa.CheckConstraint(f"status IN {STATUSES}", name="ck_product_matches_status"),
        sa.CheckConstraint(f"previous_status IN {STATUSES}", name="ck_product_matches_previous_status"),
        sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="ck_product_matches_score"),
        sa.CheckConstraint(
            "antalis_product_id IS NULL OR antalis_product_id <> product_id", name="ck_product_matches_not_self"
        ),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["antalis_product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["proposed_antalis_product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["worker_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", name="uq_product_matches_product"),
        comment="Current association of each competitor product with a reference (Antalis) product",
    )
    op.create_index("ix_product_matches_competitor_status", "product_matches", ["competitor_id", "status"])
    op.create_index("ix_product_matches_status_score", "product_matches", ["status", "score"])
    op.create_index("ix_product_matches_antalis_product", "product_matches", ["antalis_product_id"])
    op.create_index("ix_product_matches_proposed_product", "product_matches", ["proposed_antalis_product_id"])

    op.create_table(
        "product_match_candidates",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("match_id", sa.BigInteger(), nullable=False),
        sa.Column("antalis_product_id", sa.BigInteger(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True, comment="1 = best; null for excluded candidates"),
        sa.Column("score", sa.Numeric(5, 2), nullable=False),
        sa.Column("semantic_score", sa.Numeric(5, 4), nullable=True),
        sa.Column("lexical_score", sa.Numeric(5, 4), nullable=True),
        sa.Column("dimension_score", sa.Numeric(5, 4), nullable=True, comment="null = not comparable"),
        sa.Column("excluded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("details", postgresql.JSONB(), server_default=JSONB_EMPTY_OBJECT, nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["product_matches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["antalis_product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", "antalis_product_id", name="uq_product_match_candidates_pair"),
        comment="Ranked candidates of the latest computation of each match, with per-criterion scores",
    )
    op.create_index("ix_product_match_candidates_antalis_product", "product_match_candidates", ["antalis_product_id"])

    op.create_table(
        "product_match_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("match_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("competitor_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=True),
        sa.Column("from_antalis_product_id", sa.BigInteger(), nullable=True),
        sa.Column("to_antalis_product_id", sa.BigInteger(), nullable=True),
        sa.Column("score", sa.Numeric(5, 2), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False, comment="'engine' or the name of the person who decided"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "data",
            postgresql.JSONB(),
            server_default=JSONB_EMPTY_OBJECT,
            nullable=False,
            comment="Snapshot of the previous state (scores, top candidates)",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"event IN {EVENTS}", name="ck_product_match_events_event"),
        sa.ForeignKeyConstraint(["match_id"], ["product_matches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["worker_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        comment="Append-only history of product matches: computations, decisions, deprecations",
    )
    op.create_index("ix_product_match_events_match", "product_match_events", ["match_id", "created_at"])
    op.create_index("ix_product_match_events_competitor", "product_match_events", ["competitor_id", "created_at"])
    op.create_index("ix_product_match_events_event", "product_match_events", ["event", "created_at"])

    op.create_table(
        "listing_page_cache",
        sa.Column("competitor_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False, comment="requested URL"),
        sa.Column("final_url", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scrape_run_id", sa.BigInteger(), nullable=True),
        sa.Column("body", sa.LargeBinary(), nullable=False, comment="zlib-compressed UTF-8 page text"),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scrape_run_id"], ["scrape_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("competitor_id", "url"),
        comment="Listing pages of unfinished catalog syncs, replayed (no request) when a sync resumes",
    )


def downgrade() -> None:
    op.drop_table("listing_page_cache")
    op.drop_table("product_match_events")
    op.drop_table("product_match_candidates")
    op.drop_table("product_matches")
    op.drop_table("product_embeddings")
    op.execute("DELETE FROM worker_jobs WHERE task = 'matching'")
    op.drop_constraint("fk_worker_jobs_resumed_from", "worker_jobs", type_="foreignkey")
    op.drop_column("worker_jobs", "resumed_from_job_id")
    op.drop_constraint("ck_worker_jobs_task", "worker_jobs", type_="check")
    op.create_check_constraint("ck_worker_jobs_task", "worker_jobs", "task IN ('catalog_sync', 'price_refresh')")
    op.create_table(
        "product_matches",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("product_a_id", sa.BigInteger(), nullable=False),
        sa.Column("product_b_id", sa.BigInteger(), nullable=False),
        sa.Column("match_type", sa.Text(), server_default=sa.text("'equivalent'"), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'proposed'"), nullable=False),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), server_default=JSONB_EMPTY_OBJECT, nullable=False),
        sa.Column("validated_by", sa.Text(), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("status IN ('proposed', 'validated', 'rejected')", name="ck_product_matches_status"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_product_matches_conf"
        ),
        sa.CheckConstraint("product_a_id < product_b_id", name="ck_product_matches_ordered_pair"),
        sa.ForeignKeyConstraint(["product_a_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_b_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_a_id", "product_b_id", name="uq_product_matches_pair"),
    )
    op.create_index("ix_product_matches_b", "product_matches", ["product_b_id"])
