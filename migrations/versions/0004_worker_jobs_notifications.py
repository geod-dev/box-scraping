"""Worker jobs, notifications, service heartbeats; competitors remember their last full catalog sync.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB_EMPTY = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.add_column("competitors", sa.Column("last_full_catalog_at", sa.DateTime(timezone=True)))
    op.add_column("competitors", sa.Column("last_full_catalog_run_id", sa.BigInteger()))
    op.add_column("competitors", sa.Column("last_full_catalog_status", sa.Text()))
    op.add_column("competitors", sa.Column("last_price_refresh_at", sa.DateTime(timezone=True)))
    op.create_foreign_key(
        "fk_competitors_last_full_catalog_run",
        "competitors",
        "scrape_runs",
        ["last_full_catalog_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Backfill from the run history.
    op.execute(
        """
        UPDATE competitors c SET
            last_full_catalog_at = r.finished_at,
            last_full_catalog_run_id = r.id,
            last_full_catalog_status = r.status
        FROM (
            SELECT DISTINCT ON (competitor_id) competitor_id, id, finished_at, status
            FROM scrape_runs
            WHERE operation = 'catalog_discovery' AND full_scope AND status IN ('succeeded', 'partial')
            ORDER BY competitor_id, finished_at DESC
        ) r
        WHERE r.competitor_id = c.id
        """
    )
    op.execute(
        """
        UPDATE competitors c SET last_price_refresh_at = r.finished_at
        FROM (
            SELECT competitor_id, max(finished_at) AS finished_at FROM scrape_runs
            WHERE operation = 'price_refresh' AND status IN ('succeeded', 'partial')
            GROUP BY competitor_id
        ) r
        WHERE r.competitor_id = c.id
        """
    )

    op.create_table(
        "worker_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("competitor_id", sa.Integer(), sa.ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("options", postgresql.JSONB(), server_default=JSONB_EMPTY, nullable=False),
        sa.Column("requested_by", sa.Text(), server_default=sa.text("'api'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("pause_requested_at", sa.DateTime(timezone=True)),
        sa.Column("paused_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by", sa.Text()),
        sa.Column("scrape_run_id", sa.BigInteger(), sa.ForeignKey("scrape_runs.id", ondelete="SET NULL")),
        sa.Column("step", sa.Text()),
        sa.Column("step_done", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("step_goal", sa.Integer()),
        sa.Column("progress", postgresql.JSONB(), server_default=JSONB_EMPTY, nullable=False),
        sa.Column("counters", postgresql.JSONB(), server_default=JSONB_EMPTY, nullable=False),
        sa.Column("estimate", postgresql.JSONB(), server_default=JSONB_EMPTY, nullable=False),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("task IN ('catalog_sync', 'price_refresh')", name="ck_worker_jobs_task"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'succeeded', 'partial', 'failed', 'cancelled', 'interrupted')",
            name="ck_worker_jobs_status",
        ),
        comment="Worker tasks requested through the API; at most one active job per competitor",
    )
    op.create_index(
        "uq_worker_jobs_one_active_per_competitor",
        "worker_jobs",
        ["competitor_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'paused')"),
    )
    op.create_index("ix_worker_jobs_competitor_created", "worker_jobs", ["competitor_id", "created_at"])
    op.create_index("ix_worker_jobs_status", "worker_jobs", ["status"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("competitor_id", sa.Integer(), sa.ForeignKey("competitors.id", ondelete="CASCADE")),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("worker_jobs.id", ondelete="SET NULL")),
        sa.Column("scrape_run_id", sa.BigInteger(), sa.ForeignKey("scrape_runs.id", ondelete="SET NULL")),
        sa.Column("data", postgresql.JSONB(), server_default=JSONB_EMPTY, nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("level IN ('info', 'success', 'warning', 'error')", name="ck_notifications_level"),
        comment="Persisted notifications (job lifecycle, errors), read/unread",
    )
    op.create_index("ix_notifications_created", "notifications", ["created_at"])
    op.create_index(
        "ix_notifications_unread", "notifications", ["created_at"], postgresql_where=sa.text("read_at IS NULL")
    )

    op.create_table(
        "service_heartbeats",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("instance", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", postgresql.JSONB(), server_default=JSONB_EMPTY, nullable=False),
        comment="Liveness of long-running processes (the worker), shown by the API health endpoint",
    )


def downgrade() -> None:
    op.drop_table("service_heartbeats")
    op.drop_table("notifications")
    op.drop_table("worker_jobs")
    op.drop_constraint("fk_competitors_last_full_catalog_run", "competitors", type_="foreignkey")
    for column in ("last_price_refresh_at", "last_full_catalog_status", "last_full_catalog_run_id", "last_full_catalog_at"):
        op.drop_column("competitors", column)
