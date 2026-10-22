"""Retry of WAF block pages, configured per competitor.

Some sites answer HTTP 200 with a block page ("Request Rejected", "Access denied") instead of
the requested page. The markers are listed in the competitor's own config
(``options.block_page_markers``); :func:`competitor_catalog.app.build_fetcher` wraps the
Scrape.do client with :class:`BlockPageRetryFetcher` only for competitors that list some.
Other competitors keep the plain client.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from competitor_catalog.http.base import FetchedPage, Fetcher, TransientFetchError

log = logging.getLogger(__name__)


class BlockPageRetryFetcher:
    """Fetches through ``inner`` and re-fetches while the body contains a block marker.

    The last attempt that still shows a block page raises :class:`TransientFetchError`, so the
    item is recorded as failed, never as an empty or not-found page.
    """

    def __init__(
        self,
        inner: Fetcher,
        markers: Iterable[str],
        *,
        max_attempts: int = 4,
        backoff_base_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._inner = inner
        self.markers = tuple(str(m) for m in markers)
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base_seconds
        self._sleep = sleep

    def fetch(self, url: str) -> FetchedPage:
        for attempt in range(1, self.max_attempts + 1):
            page = self._inner.fetch(url)
            if not any(marker in page.text for marker in self.markers):
                return page
            log.warning("fetch %s attempt %d/%d returned a block page", url, attempt, self.max_attempts)
            if attempt < self.max_attempts:
                self._sleep(self.backoff_base * 2 ** (attempt - 1))
        raise TransientFetchError(f"block page returned {self.max_attempts} times", url=url)

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> BlockPageRetryFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        # Everything else (stats, options, account_info...) is the wrapped client's.
        if name == "_inner":  # not set yet (copy, pickle): raise instead of recursing forever
            raise AttributeError(name)
        return getattr(self._inner, name)
