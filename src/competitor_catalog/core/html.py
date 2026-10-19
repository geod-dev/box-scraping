"""Small lxml helpers shared by HTML-based adapters."""

from __future__ import annotations

import html as html_lib
import json
import logging
from collections.abc import Iterator
from typing import Any

import lxml.html
from lxml.html import HtmlElement

from competitor_catalog.core.normalize import clean_text

log = logging.getLogger(__name__)

BLOCK_TAGS = frozenset({"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol", "section"})


def parse_html(text: str) -> HtmlElement:
    return lxml.html.fromstring(text)


def text_of(el: HtmlElement | None) -> str | None:
    """Whitespace-collapsed text content; ``None`` if the element is missing."""
    if el is None:
        return None
    return clean_text(el.text_content())


def block_text(el: HtmlElement | None) -> str | None:
    """Text content preserving paragraph/list boundaries as newlines."""
    if el is None:
        return None
    parts: list[str] = []

    def walk(node: HtmlElement) -> None:
        tag = node.tag.lower() if isinstance(node.tag, str) else ""
        if tag in ("script", "style"):
            if node.tail:
                parts.append(node.tail)
            return
        if tag in BLOCK_TAGS:
            parts.append("\n")
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
        if tag in BLOCK_TAGS:
            parts.append("\n")
        if node.tail:
            parts.append(node.tail)

    walk(el)
    lines = [clean_text(line) for line in "".join(parts).split("\n")]
    return "\n".join(line for line in lines if line)


def first(el: HtmlElement, selector: str) -> HtmlElement | None:
    found = el.cssselect(selector)
    return found[0] if found else None


def attr(el: HtmlElement | None, name: str) -> str | None:
    """Attribute value; lxml's HTML parser lower-cases attribute names."""
    if el is None:
        return None
    value = el.get(name)
    if value is None:
        value = el.get(name.lower())
    return value


def json_attr(el: HtmlElement | None, name: str) -> Any:
    raw = attr(el, name)
    if raw is None:
        return None
    try:
        return json.loads(html_lib.unescape(raw))
    except (ValueError, TypeError):
        return None


def iter_json_ld(doc: HtmlElement) -> Iterator[dict[str, Any]]:
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        raw = script.text or ""
        try:
            data = json.loads(raw, strict=False)  # some sites leave raw newlines in strings
        except ValueError:
            log.debug("invalid JSON-LD block ignored")
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                yield from _expand_graph(item)


def _expand_graph(item: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if isinstance(item.get("@graph"), list):
        for sub in item["@graph"]:
            if isinstance(sub, dict):
                yield sub
    else:
        yield item


def json_ld_of_type(doc: HtmlElement, type_name: str) -> dict[str, Any] | None:
    for item in iter_json_ld(doc):
        types = item.get("@type")
        if types == type_name or (isinstance(types, list) and type_name in types):
            return item
    return None


def canonical_url(doc: HtmlElement) -> str | None:
    link = doc.xpath('//link[@rel="canonical"]/@href')
    return link[0].strip() if link else None
