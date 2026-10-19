"""Competitor configuration: one YAML file per competitor in ``config/competitors/``.

Everything that does not require code lives here (URLs, rate limits, Scrape.do options,
label mappings...). The ``adapter`` key is an import path ``package.module:ClassName`` so
that adding a competitor never requires touching the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CompetitorConfig:
    key: str
    name: str
    base_url: str
    adapter: str
    root_urls: list[str]
    enabled: bool = True
    request_interval_seconds: float = 10.0
    max_attempts: int = 4
    default_currency: str = "EUR"
    scrapedo: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)  # adapter-specific settings
    # Expected catalog size, used to estimate durations before any run history exists:
    # {"listing_pages": int, "products": int}
    estimate_hints: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc.lower()

    def as_snapshot(self) -> dict[str, Any]:
        """Serializable copy stored on the ``competitors`` row for traceability."""
        return {
            "root_urls": self.root_urls,
            "request_interval_seconds": self.request_interval_seconds,
            "max_attempts": self.max_attempts,
            "default_currency": self.default_currency,
            "scrapedo": self.scrapedo,
            "options": self.options,
            "source_path": str(self.source_path) if self.source_path else None,
        }


_REQUIRED = ("key", "name", "base_url", "adapter", "root_urls")


def parse_competitor_config(data: dict[str, Any], source_path: Path | None = None) -> CompetitorConfig:
    missing = [k for k in _REQUIRED if not data.get(k)]
    if missing:
        raise ConfigError(f"{source_path or 'config'}: missing required keys {missing}")
    if ":" not in str(data["adapter"]):
        raise ConfigError(f"{source_path}: 'adapter' must be an import path 'module:Class'")
    root_urls = data["root_urls"]
    if isinstance(root_urls, str):
        root_urls = [root_urls]
    base_host = urlsplit(data["base_url"]).netloc.lower()
    for url in root_urls:
        if urlsplit(url).netloc.lower() != base_host:
            raise ConfigError(f"{source_path}: root url {url!r} is not on {base_host}")
    known = set(_REQUIRED) | {
        "enabled",
        "request_interval_seconds",
        "max_attempts",
        "default_currency",
        "scrapedo",
        "options",
        "estimate_hints",
    }
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{source_path}: unknown keys {sorted(unknown)}")
    return CompetitorConfig(
        key=str(data["key"]),
        name=str(data["name"]),
        base_url=str(data["base_url"]).rstrip("/"),
        adapter=str(data["adapter"]),
        root_urls=[str(u) for u in root_urls],
        enabled=bool(data.get("enabled", True)),
        request_interval_seconds=float(data.get("request_interval_seconds", 10.0)),
        max_attempts=int(data.get("max_attempts", 4)),
        default_currency=str(data.get("default_currency", "EUR")),
        scrapedo=dict(data.get("scrapedo") or {}),
        options=dict(data.get("options") or {}),
        estimate_hints=dict(data.get("estimate_hints") or {}),
        source_path=source_path,
    )


def load_competitor_configs(directory: Path) -> dict[str, CompetitorConfig]:
    if not directory.is_dir():
        raise ConfigError(f"competitor config directory not found: {directory}")
    configs: dict[str, CompetitorConfig] = {}
    for path in sorted(directory.glob("*.y*ml")):
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        config = parse_competitor_config(data, path)
        if config.key in configs:
            raise ConfigError(f"duplicate competitor key {config.key!r} ({path})")
        configs[config.key] = config
    return configs


def load_competitor_config(directory: Path, key: str) -> CompetitorConfig:
    configs = load_competitor_configs(directory)
    try:
        return configs[key]
    except KeyError:
        raise ConfigError(f"unknown competitor {key!r}; known: {sorted(configs)}") from None
