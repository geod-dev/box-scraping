"""Process-level settings read from the environment (optionally from a local .env file).

Secrets (the Scrape.do and Voyage AI API keys) are only ever read from the environment and are never
logged: see :mod:`competitor_catalog.logging_setup` for the redaction filter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMPETITOR_CONFIG_DIR = PROJECT_ROOT / "config" / "competitors"
DEFAULT_MATCHING_CONFIG = PROJECT_ROOT / "config" / "matching.yaml"


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    scrapedo_api_key: str | None
    competitor_config_dir: Path
    log_level: str
    html_archive_dir: Path | None
    voyage_api_key: str | None = None  # product matching embeddings (Voyage AI)
    matching_config_path: Path = DEFAULT_MATCHING_CONFIG

    def require_database_url(self) -> str:
        if not self.database_url:
            raise SettingsError("DATABASE_URL is not set (see .env.example).")
        return self.database_url

    def require_scrapedo_api_key(self) -> str:
        if not self.scrapedo_api_key:
            raise SettingsError("SCRAPEDO_API_KEY is not set (see .env.example).")
        return self.scrapedo_api_key


def load_settings(env_file: str | os.PathLike[str] | None = None) -> Settings:
    # Real environment variables win over the .env file.
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    archive = os.environ.get("HTML_ARCHIVE_DIR") or None
    return Settings(
        database_url=os.environ.get("DATABASE_URL") or None,
        scrapedo_api_key=os.environ.get("SCRAPEDO_API_KEY") or None,
        competitor_config_dir=Path(os.environ.get("COMPETITOR_CONFIG_DIR") or DEFAULT_COMPETITOR_CONFIG_DIR),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        html_archive_dir=Path(archive) if archive else None,
        voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
        matching_config_path=Path(os.environ.get("MATCHING_CONFIG") or DEFAULT_MATCHING_CONFIG),
    )
