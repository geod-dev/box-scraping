"""Contract between the generic engine and competitor-specific adapters.

An adapter knows *how to read one website*: classify a fetched page, extract the
navigation (categories, product links, pagination), product details and prices. It does
no I/O and knows nothing about the database: the engine fetches pages, calls the adapter,
and persists the returned :mod:`competitor_catalog.core.types` objects.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod

from competitor_catalog.core.config import CompetitorConfig, ConfigError
from competitor_catalog.core.types import (
    ListingPage,
    PageKind,
    ProductData,
    ProductPricePage,
)
from competitor_catalog.core.urls import normalize_url
from competitor_catalog.http.base import FetchedPage


class CompetitorAdapter(ABC):
    def __init__(self, config: CompetitorConfig):
        self.config = config

    @abstractmethod
    def classify(self, page: FetchedPage) -> PageKind:
        """Tell listing pages from product pages.

        Must raise :class:`~competitor_catalog.core.errors.UnexpectedPageError` when the
        response is not a recognisable page of the site (block page, error page...).
        """

    @abstractmethod
    def parse_listing(self, page: FetchedPage) -> ListingPage:
        """Category/listing/family page -> category info, children, product links, next page."""

    @abstractmethod
    def parse_product(self, page: FetchedPage) -> ProductData:
        """Product detail page -> full product data *including* prices."""

    @abstractmethod
    def parse_prices(self, page: FetchedPage) -> ProductPricePage:
        """Product detail page -> identity + prices only (used by the price refresh)."""

    def normalize_url(self, url: str, base: str | None = None) -> str:
        """Canonical form used for de-duplication. Override for site-specific rules."""
        return normalize_url(url, base or self.config.base_url)

    def is_in_scope(self, category_url: str, ancestors_urls: list[str], root_urls: set[str]) -> bool:
        """A category belongs to the crawl when it, or one of its navigation ancestors, is a root."""
        return category_url in root_urls or any(u in root_urls for u in ancestors_urls)


def load_adapter(config: CompetitorConfig) -> CompetitorAdapter:
    module_name, _, class_name = config.adapter.partition(":")
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(f"cannot load adapter {config.adapter!r}: {exc}") from exc
    if not (isinstance(cls, type) and issubclass(cls, CompetitorAdapter)):
        raise ConfigError(f"{config.adapter!r} is not a CompetitorAdapter subclass")
    return cls(config)
