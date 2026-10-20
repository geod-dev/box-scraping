"""Small reusable Scrape.do API client.

Request format (https://scrape.do/documentation/): ``GET https://api.scrape.do/?token=...&url=<encoded>``
plus optional parameters (``render``, ``super``, ``geoCode``, ``timeout``...).

Status codes returned by Scrape.do in default (non-transparent) mode:

* 2xx  success (billed)                      -> page returned
* 404  target not found (billed)             -> :class:`TargetGoneError`
* 400  bad request (Scrape.do or target)     -> :class:`FetchError` (not retried)
* 401  no credits / suspended / bad token    -> :class:`FatalFetchError` (run must stop)
* 429  concurrency limit exceeded (free)     -> retried with backoff
* 502  request failed, try again (free)      -> retried with backoff
* 510  request cancelled (free)              -> retried with backoff

Useful response headers: ``Scrape.do-Request-Cost`` (authoritative credit cost),
``Scrape.do-Remaining-Credits``, ``Scrape.do-Initial-Status-Code``, ``Scrape.do-Resolved-Url``.

The token is passed as a query parameter (that is how the API authenticates), so the full
request URL is never logged nor put in exception messages.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from competitor_catalog.http.base import (
    FatalFetchError,
    FetchedPage,
    FetchError,
    TargetGoneError,
    TransientFetchError,
)
from competitor_catalog.http.ratelimit import MinIntervalRateLimiter
from competitor_catalog.logging_setup import redact, register_secret

log = logging.getLogger(__name__)

API_ENDPOINT = "https://api.scrape.do/"
INFO_ENDPOINT = "https://api.scrape.do/info"
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504, 510})


@dataclass
class ScrapeDoOptions:
    """Per-competitor request options (see ``scrapedo:`` in the competitor YAML)."""

    render: bool = False
    super_proxy: bool = False
    geo_code: str | None = None
    timeout_ms: int = 60_000  # Scrape.do accepts 5_000..120_000
    extra_params: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object] | None) -> ScrapeDoOptions:
        data = dict(data or {})
        geo_code = data.pop("geo_code", None)
        return cls(
            render=bool(data.pop("render", False)),
            super_proxy=bool(data.pop("super", False)),
            geo_code=str(geo_code) if geo_code else None,
            timeout_ms=int(data.pop("timeout_ms", 60_000)),
            extra_params={str(k): str(v) for k, v in data.items()},
        )


@dataclass
class ScrapeDoStats:
    requests: int = 0
    successes: int = 0
    failures: int = 0
    retries: int = 0
    credits_used: int = 0
    remaining_credits: int | None = None


class ScrapeDoClient:
    def __init__(
        self,
        api_key: str,
        *,
        options: ScrapeDoOptions | None = None,
        rate_limiter: MinIntervalRateLimiter | None = None,
        max_attempts: int = 4,
        backoff_base_seconds: float = 2.0,
        backoff_max_seconds: float = 60.0,
        http_client: httpx.Client | None = None,
        archive_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ValueError("Scrape.do API key is required")
        register_secret(api_key)
        self._api_key = api_key
        self.options = options or ScrapeDoOptions()
        self.rate_limiter = rate_limiter or MinIntervalRateLimiter(0)
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base_seconds
        self.backoff_max = backoff_max_seconds
        # Read timeout must exceed Scrape.do's own target timeout; the connect timeout is
        # short so that a dropped connection attempt is retried quickly.
        timeout = httpx.Timeout(self.options.timeout_ms / 1000 + 30, connect=10.0)
        # gzip/deflate only: httpx cannot decode brotli without the optional dependency.
        self._http = http_client or httpx.Client(timeout=timeout, headers={"Accept-Encoding": "gzip, deflate"})
        self._archive_dir = archive_dir
        self._sleep = sleep
        self.stats = ScrapeDoStats()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ScrapeDoClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ public API
    def fetch(self, url: str) -> FetchedPage:
        params = self._params(url)
        last_error: FetchError | None = None
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                self.stats.retries += 1
            self.rate_limiter.wait()
            self.stats.requests += 1
            started = time.monotonic()
            try:
                response = self._http.get(API_ENDPOINT, params=params)
            except httpx.HTTPError as exc:  # timeouts, connection resets, protocol errors
                last_error = TransientFetchError(redact(f"{type(exc).__name__}: {exc}"), url=url, status_code=None)
                log.warning("fetch %s attempt %d/%d failed: %s", url, attempt, self.max_attempts, last_error)
                self._backoff(attempt, None)
                continue

            elapsed = time.monotonic() - started
            self._account(response)
            status = response.status_code
            if 200 <= status < 300:
                self.stats.successes += 1
                page = self._to_page(url, response)
                log.debug("fetched %s status=%s cost=%s in %.1fs", url, status, page.meta.get("request_cost"), elapsed)
                self._archive(page)
                return page

            detail = redact(response.text[:300].strip().replace("\n", " "))
            if status == 401:
                self.stats.failures += 1
                raise FatalFetchError(
                    f"Scrape.do rejected the request (401: no credits, suspended account or invalid token): {detail}",
                    url=url,
                    status_code=status,
                )
            if status in (404, 410):
                self.stats.failures += 1
                raise TargetGoneError(f"target returned {status}", url=url, status_code=status)
            if status in RETRYABLE_STATUSES:
                last_error = TransientFetchError(f"Scrape.do status {status}: {detail}", url=url, status_code=status)
                log.warning("fetch %s attempt %d/%d got status %d", url, attempt, self.max_attempts, status)
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            self.stats.failures += 1
            raise FetchError(f"non-retryable status {status}: {detail}", url=url, status_code=status)

        self.stats.failures += 1
        assert last_error is not None
        raise last_error

    def account_info(self) -> dict[str, object]:
        """Subscription/usage info (endpoint limited to 10 requests/minute)."""
        for attempt in range(1, 4):
            try:
                response = self._http.get(INFO_ENDPOINT, params={"token": self._api_key})
                break
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise TransientFetchError(redact(f"{type(exc).__name__}: {exc}"), url=INFO_ENDPOINT) from None
                self._sleep(self.backoff_base * attempt)
        if response.status_code != 200:
            raise FetchError(
                f"Scrape.do info endpoint returned {response.status_code}",
                url=INFO_ENDPOINT,
                status_code=response.status_code,
            )
        return response.json()

    # ------------------------------------------------------------------ internals
    def _params(self, url: str) -> dict[str, str]:
        opts = self.options
        params: dict[str, str] = {"token": self._api_key, "url": url, "timeout": str(opts.timeout_ms)}
        if opts.render:
            params["render"] = "true"
        if opts.super_proxy:
            params["super"] = "true"
        if opts.geo_code:
            params["geoCode"] = opts.geo_code
        params.update(opts.extra_params)
        return params

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= self.max_attempts:
            return
        delay = min(self.backoff_max, self.backoff_base * 2 ** (attempt - 1))
        if retry_after and retry_after.isdigit():
            delay = max(delay, min(float(retry_after), 300.0))  # honour Retry-After, within reason
        self._sleep(delay + random.uniform(0, delay * 0.25))

    def _account(self, response: httpx.Response) -> None:
        cost = _int_header(response, "Scrape.do-Request-Cost")
        if cost:
            self.stats.credits_used += cost
        remaining = _int_header(response, "Scrape.do-Remaining-Credits")
        if remaining is not None:
            self.stats.remaining_credits = remaining

    def _to_page(self, url: str, response: httpx.Response) -> FetchedPage:
        headers = response.headers
        meta: dict[str, object] = {
            "provider": "scrape.do",
            "request_cost": _int_header(response, "Scrape.do-Request-Cost"),
            "initial_status_code": _int_header(response, "Scrape.do-Initial-Status-Code"),
            "request_id": headers.get("Scrape.do-Request-Id"),
            "content_type": headers.get("Content-Type"),
            "detected_waf": headers.get("Scrape.do-Detected-Waf"),
        }
        return FetchedPage(
            requested_url=url,
            final_url=headers.get("Scrape.do-Resolved-Url") or url,
            status_code=response.status_code,
            text=_decode(response),
            fetched_at=datetime.now(UTC),
            meta=meta,
        )

    def _archive(self, page: FetchedPage) -> None:
        if not self._archive_dir:
            return
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha1(page.requested_url.encode()).hexdigest()[:16] + ".html"
        (self._archive_dir / name).write_text(
            f"<!-- source: {page.requested_url} fetched_at: {page.fetched_at.isoformat()} -->\n" + page.text,
            encoding="utf-8",
        )


def _decode(response: httpx.Response) -> str:
    """Body as text. Without a charset in Content-Type httpx guesses a legacy encoding
    (mojibake or U+FFFD on JSON APIs that send UTF-8), so UTF-8 is the default here."""
    return response.content.decode(response.charset_encoding or "utf-8", errors="replace")


def _int_header(response: httpx.Response, name: str) -> int | None:
    value = response.headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
