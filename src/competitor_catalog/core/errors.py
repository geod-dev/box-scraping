"""Parsing errors raised by adapters; the engine records them per page/product."""

from __future__ import annotations


class PageError(Exception):
    def __init__(self, message: str, *, url: str | None = None):
        super().__init__(message)
        self.url = url


class UnexpectedPageError(PageError):
    """The response is not a page of the expected site/type.

    Typical causes: anti-bot challenge, error page served with HTTP 200, truncated body,
    redirect to another kind of page. Never interpreted as "empty category".
    """


class PageParseError(PageError):
    """The page was recognised but mandatory data could not be extracted."""
