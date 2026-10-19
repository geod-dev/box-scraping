"""Schema migrations, serialized with a PostgreSQL advisory lock (API and worker both
migrate at start-up; the second one waits, then finds the schema up to date)."""

from __future__ import annotations

from sqlalchemy import text

from competitor_catalog.db.session import make_engine
from competitor_catalog.settings import PROJECT_ROOT

MIGRATION_LOCK_ID = 727_100_001


def upgrade_database(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    cfg.attributes["db_url"] = database_url
    engine = make_engine(database_url)
    try:
        with engine.connect() as lock_conn:
            lock_conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
            try:
                command.upgrade(cfg, "head")
            finally:
                lock_conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
    finally:
        engine.dispose()
