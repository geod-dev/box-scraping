from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# Query parameters that never change page content (analytics/tracking).
TRACKING_PARAMS = frozenset({"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"})


def normalize_url(url: str, base: str | None = None) -> str:
    """Absolute URL with lower-cased scheme/host, no fragment, no tracking params,
    sorted query string and no trailing slash (except for the site root)."""
    url = url.strip()
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in TRACKING_PARAMS)
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def same_host(url: str, host: str) -> bool:
    return urlsplit(url).netloc.lower() == host.lower()


def last_path_segment(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
