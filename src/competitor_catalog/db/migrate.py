"""Schema migrations, serialized with a PostgreSQL advisory lock (API and worker both
migrate at start-up; the second one waits, then finds the schema up to date).

Rule for every migration: stay compatible with the previous release, which keeps running
while a deploy replaces it (the old worker finishes its current page). Add first; drop what
the code no longer uses in a later release. :func:`schema_is_newer` lets an old worker see
that a newer release migrated the database, so that it stops starting jobs."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.orm import Session

from competitor_catalog.db.session import make_engine
from competitor_catalog.settings import PROJECT_ROOT

MIGRATION_LOCK_ID = 727_100_001


def _alembic_config():
    from alembic.config import Config

    return Config(str(PROJECT_ROOT / "alembic.ini"))


@lru_cache(maxsize=1)
def known_revisions() -> frozenset[str]:
    """Every migration revision this code knows."""
    from alembic.script import ScriptDirectory

    return frozenset(rev.revision for rev in ScriptDirectory.from_config(_alembic_config()).walk_revisions())


def schema_is_newer(session: Session) -> bool:
    """True when the database was migrated by a newer release than this code."""
    version = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return version is not None and version not in known_revisions()


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
