"""The migrations build exactly the schema declared by the SQLAlchemy models."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from competitor_catalog.db.models import Base

pytestmark = pytest.mark.db


def test_migrations_match_the_models(migrated_engine):
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
