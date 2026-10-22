"""Block-page retry: configured per competitor (options.block_page_markers), outside scrapedo.py."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from competitor_catalog.app import build_fetcher
from competitor_catalog.core.config import load_competitor_config
from competitor_catalog.http.base import TransientFetchError
from competitor_catalog.http.block_pages import BlockPageRetryFetcher
from competitor_catalog.http.scrapedo import ScrapeDoClient
from competitor_catalog.settings import DEFAULT_COMPETITOR_CONFIG_DIR
from tests.conftest import make_page

MARKER = "<title>Request Rejected</title>"
BLOCK = f"<html>{MARKER}</html>"
URL = "https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/categories/Cenpac/1000"


class ScriptedFetcher:
    """Stands in for ScrapeDoClient: returns the scripted bodies in order (last one repeats)."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls: list[str] = []

    def fetch(self, url):
        self.calls.append(url)
        body = self.bodies[min(len(self.calls) - 1, len(self.bodies) - 1)]
        return make_page(body, url)


def test_block_marker_page_is_retried():
    inner = ScriptedFetcher([BLOCK, '{"type":"Category"}'])
    sleeps: list[float] = []
    fetcher = BlockPageRetryFetcher(inner, [MARKER], max_attempts=4, sleep=sleeps.append)
    assert fetcher.fetch(URL).text == '{"type":"Category"}'
    assert len(inner.calls) == 2
    assert len(sleeps) == 1


def test_block_marker_page_every_time_is_a_transient_error():
    inner = ScriptedFetcher([BLOCK])
    fetcher = BlockPageRetryFetcher(inner, [MARKER], max_attempts=3, sleep=lambda _s: None)
    with pytest.raises(TransientFetchError):
        fetcher.fetch(URL)
    assert len(inner.calls) == 3


def test_without_markers_a_block_page_is_returned():
    inner = ScriptedFetcher([BLOCK])
    fetcher = BlockPageRetryFetcher(inner, [], max_attempts=3, sleep=lambda _s: None)
    assert fetcher.fetch(URL).text == BLOCK
    assert len(inner.calls) == 1


def test_other_attributes_are_the_wrapped_fetchers():
    inner = ScriptedFetcher([BLOCK])
    fetcher = BlockPageRetryFetcher(inner, [MARKER], sleep=lambda _s: None)
    assert fetcher.calls is inner.calls


def test_cenpac_config_declares_the_block_markers():
    config = load_competitor_config(DEFAULT_COMPETITOR_CONFIG_DIR, "cenpac_fr")
    assert config.options["block_page_markers"] == [MARKER]
    # the markers are not Scrape.do request parameters
    assert "block_page_markers" not in config.scrapedo


def test_wrapper_can_be_copied_without_recursion():
    fetcher = BlockPageRetryFetcher(ScriptedFetcher([BLOCK]), [MARKER], sleep=lambda _s: None)
    clone = copy.copy(fetcher)
    assert clone.markers == fetcher.markers


def test_build_fetcher_wraps_only_competitors_with_block_page_markers(tmp_path):
    settings = SimpleNamespace(require_scrapedo_api_key=lambda: "key", html_archive_dir=tmp_path)
    cenpac = load_competitor_config(DEFAULT_COMPETITOR_CONFIG_DIR, "cenpac_fr")
    packdiscount = load_competitor_config(DEFAULT_COMPETITOR_CONFIG_DIR, "packdiscount_fr")
    with build_fetcher(settings, cenpac) as wrapped:
        assert isinstance(wrapped, BlockPageRetryFetcher)
        assert wrapped.markers == (MARKER,)
    with build_fetcher(settings, packdiscount) as plain:
        assert isinstance(plain, ScrapeDoClient)
        assert not isinstance(plain, BlockPageRetryFetcher)
