"""Transport-agnostic fetch contract used by the scraping engine.

The engine only depends on :class:`Fetcher`; Scrape.do is one implementation, a fixture
server used by the tests is another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status_code: int
    text: str
    fetched_at: datetime
    # Provider metadata (credits, initial status code, request id...). Never contains secrets.
    meta: dict[str, object] = field(default_factory=dict)


class FetchError(Exception):
    """Base class. ``retryable`` tells the caller whether a later attempt may succeed."""

    retryable = False

    def __init__(self, message: str, *, url: str, status_code: int | None = None):
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class TransientFetchError(FetchError):
    """Network error, timeout, proxy failure, rate limiting... retries were exhausted."""

    retryable = True


class TargetGoneError(FetchError):
    """The target answered 404/410: the page does not exist (anymore)."""


class FatalFetchError(FetchError):
    """Configuration/account problem (bad token, no credits...). The run must stop."""


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage: ...
