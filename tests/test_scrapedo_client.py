from __future__ import annotations

import logging
import random

import httpx
import pytest

from competitor_catalog.http.base import FatalFetchError, FetchError, TargetGoneError, TransientFetchError
from competitor_catalog.http.ratelimit import MinIntervalRateLimiter
from competitor_catalog.http.scrapedo import ScrapeDoClient, ScrapeDoOptions
from competitor_catalog.logging_setup import RedactingFilter, redact

TOKEN = "test-token-1234567890abcdef"
TARGET = "https://www.antalis.fr/eshop/x?no=20"


def make_client(responses, *, options=None, max_attempts=3):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    client = ScrapeDoClient(
        TOKEN,
        options=options,
        max_attempts=max_attempts,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None,
    )
    return client, calls


def ok(body="<html>ok</html>", **headers):
    return httpx.Response(200, text=body, headers={"Scrape.do-Request-Cost": "1", **headers})


def test_success_and_request_format():
    client, calls = make_client(
        [ok(**{"Scrape.do-Resolved-Url": "https://www.antalis.fr/final", "Scrape.do-Initial-Status-Code": "301"})],
        options=ScrapeDoOptions.from_mapping({"render": False, "geo_code": "fr", "timeout_ms": 30000}),
    )
    page = client.fetch(TARGET)
    assert page.text == "<html>ok</html>"
    assert page.final_url == "https://www.antalis.fr/final"
    assert page.meta["initial_status_code"] == 301
    request = calls[0]
    assert request.url.host == "api.scrape.do"
    params = dict(request.url.params)
    assert params == {"token": TOKEN, "url": TARGET, "timeout": "30000", "geoCode": "fr"}  # url is encoded by httpx
    assert "render" not in params
    assert client.stats.credits_used == 1


def test_render_and_super_flags():
    client, calls = make_client([ok()], options=ScrapeDoOptions.from_mapping({"render": True, "super": True}))
    client.fetch(TARGET)
    params = dict(calls[0].url.params)
    assert params["render"] == "true" and params["super"] == "true"


@pytest.mark.parametrize("status", [429, 502, 510])
def test_transient_statuses_are_retried(status):
    client, calls = make_client([httpx.Response(status, text="busy"), ok()])
    assert client.fetch(TARGET).text == "<html>ok</html>"
    assert len(calls) == 2
    assert client.stats.retries == 1


def test_network_errors_are_retried_then_reported():
    client, calls = make_client([httpx.ConnectTimeout("timed out")], max_attempts=3)
    with pytest.raises(TransientFetchError):
        client.fetch(TARGET)
    assert len(calls) == 3


def test_401_is_fatal_and_not_retried():
    client, calls = make_client([httpx.Response(401, text="no credits")])
    with pytest.raises(FatalFetchError):
        client.fetch(TARGET)
    assert len(calls) == 1


def test_404_means_target_gone():
    client, _ = make_client([httpx.Response(404, text="not found")])
    with pytest.raises(TargetGoneError) as info:
        client.fetch(TARGET)
    assert info.value.status_code == 404


def test_400_is_not_retried():
    client, calls = make_client([httpx.Response(400, text="bad")])
    with pytest.raises(FetchError) as info:
        client.fetch(TARGET)
    assert not info.value.retryable
    assert len(calls) == 1


def test_token_never_appears_in_errors_or_logs():
    # an error body echoing the request URL (token included) must be redacted
    echo = httpx.Response(400, text=f"bad request https://api.scrape.do/?token={TOKEN}&url=x")
    client, _ = make_client([echo])
    with pytest.raises(FetchError) as info:
        client.fetch(TARGET)
    assert TOKEN not in str(info.value)

    record = logging.getLogger("t").makeRecord(
        "t", logging.INFO, __file__, 1, "url=%s", (f"https://api.scrape.do/?token={TOKEN}",), None
    )
    RedactingFilter().filter(record)
    assert TOKEN not in record.getMessage()
    assert redact(f"token={TOKEN}&x=1") == "token=***&x=1"


def test_rate_limiter_enforces_min_interval():
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    limiter = MinIntervalRateLimiter(10, clock=lambda: now[0], sleep=sleep)
    limiter.wait()
    now[0] += 3
    limiter.wait()
    assert slept == [7]


def _gaps(interval: float, jitter: float, count: int, seed: int = 42) -> list[float]:
    """Sleep durations of ``count`` back-to-back waits (the clock only moves while sleeping,
    so every slept amount is the gap drawn for that request start)."""
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    limiter = MinIntervalRateLimiter(
        interval, jitter_seconds=jitter, rng=random.Random(seed), clock=lambda: now[0], sleep=sleep
    )
    for _ in range(count + 1):  # the first wait never sleeps
        limiter.wait()
    return slept


def test_rate_limiter_jitter_stays_within_bounds_and_varies():
    gaps = _gaps(3.0, 1.0, 300)
    assert len(gaps) == 300
    assert all(2.0 <= g <= 4.0 for g in gaps)
    assert len({round(g, 6) for g in gaps}) > 250  # not a fixed cadence
    assert min(gaps) < 2.2 and max(gaps) > 3.8  # both ends of the range are reached


def test_rate_limiter_jitter_is_clamped_at_zero():
    gaps = _gaps(0.5, 2.0, 300)
    assert all(0.0 < g <= 2.5 for g in gaps)
    # Draws below -0.5 clamp to a zero gap, which means no sleep at all.
    assert len(gaps) < 300


def test_rate_limiter_jitter_is_reproducible_with_a_seed():
    assert _gaps(3.0, 1.0, 20, seed=7) == _gaps(3.0, 1.0, 20, seed=7)


def test_rate_limiter_without_jitter_keeps_the_fixed_interval():
    assert _gaps(3.0, 0.0, 5) == [3.0] * 5
