"""Offline export of the OpenAPI schema (no database needed): ``catalog-scraper export-openapi``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from competitor_catalog.api.main import create_app
from competitor_catalog.settings import load_settings


def openapi_schema() -> dict[str, Any]:
    app = create_app(load_settings(), sessionmaker(), api_key="export", sync_competitors=False)
    return app.openapi()


def export_openapi(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(openapi_schema(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
