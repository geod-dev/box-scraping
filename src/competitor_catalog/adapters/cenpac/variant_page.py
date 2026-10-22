"""Cenpac variant web pages (HTML): labelled attributes, descriptions, images, category, family.

Pure functions, no adapter state. The price block is read by :mod:`.prices`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from lxml.html import HtmlElement

from competitor_catalog.core.errors import PageParseError, UnexpectedPageError
from competitor_catalog.core.html import block_text, canonical_url, first, json_ld_of_type, parse_html, text_of
from competitor_catalog.core.normalize import clean_text

DATA_LAYER_START = re.compile(r"var dataLayer = \[gtmHelper\.getData\(")
BLOCK_MARKER = "<title>Request Rejected</title>"
VARIANT_TEMPLATE = "Produit_sku"


def data_layer(text: str) -> dict | None:
    """The JSON object passed to ``gtmHelper.getData``. Decoded as JSON, so a ``})]`` inside a string
    value does not end it early."""
    match = DATA_LAYER_START.search(text)
    if not match:
        return None
    try:
        data, end = json.JSONDecoder().raw_decode(text, match.end())
    except ValueError:
        return None
    if text[end : end + 2] != ")]" or not isinstance(data, dict):
        return None
    return data


def page_template(text: str) -> str | None:
    return ((data_layer(text) or {}).get("page") or {}).get("env_template")


@dataclass
class VariantPage:
    doc: HtmlElement
    sku: str
    title: str | None
    description: str | None  # family tagline (h2 under the h1); "" = shown empty
    long_description: str | None  # "Description détaillée" text; None = section absent
    attributes: list[tuple[str, str | None]]  # (label without " :", value)
    images: list[dict] | None
    master_sku: str | None
    family_name: str | None
    category_path: list[tuple[str, str | None]]  # [(id, name)] from the dataLayer, root excluded


def read_variant_page(text: str, url: str) -> VariantPage:
    if BLOCK_MARKER in text:
        raise UnexpectedPageError("Cenpac access-denied page", url=url)
    template = page_template(text)
    if template != VARIANT_TEMPLATE:
        raise UnexpectedPageError(f"not a Cenpac variant page (template {template!r})", url=url)

    doc = parse_html(text)
    sku = text_of(first(doc, 'span[itemprop="productID"]'))
    if not sku:
        raise PageParseError("no product reference", url=url)
    layer = data_layer(text) or {}
    remarketing = layer.get("Remarketing_Display") or {}
    if "ecomm_prodid" in remarketing and str(remarketing["ecomm_prodid"]) != sku:
        raise PageParseError(f"dataLayer product {remarketing['ecomm_prodid']!r} differs from {sku!r}", url=url)

    h1 = first(doc, 'h1[itemprop="name"]')
    if h1 is None:
        raise PageParseError("no product title", url=url)
    title = text_of(h1)
    info = h1.getnext()  # the div.flex-auto that follows the title

    description: str | None = None
    attributes: list[tuple[str, str | None]] = []
    if info is not None:
        h2 = first(info, "h2")
        description = text_of(h2) if h2 is not None else None
        for li in info.xpath(".//ul/li[strong]"):
            strong = li.find("strong")
            label = clean_text(strong.text_content()).removesuffix(":").strip()
            value = clean_text(
                (strong.tail or "") + "".join(x.text_content() + (x.tail or "") for x in strong.itersiblings())
            )
            attributes.append((label, value))

    long_description: str | None = None
    sections = doc.xpath(
        '//section[.//p[normalize-space()="Description détaillée"]]//section[contains(@class,"md:col-8")]'
    )
    if sections:
        divs = sections[0].xpath("./div")
        long_description = (block_text(divs[1]) or "") if len(divs) > 1 else ""

    images = _images(doc)
    master_sku = _master_sku(doc)
    if master_sku == sku:
        master_sku = None

    return VariantPage(
        doc=doc,
        sku=sku,
        title=title,
        description=description,
        long_description=long_description,
        attributes=attributes,
        images=images,
        master_sku=master_sku,
        family_name=_family_name(doc, master_sku),
        category_path=_category_path(layer),
    )


def _images(doc: HtmlElement) -> list[dict[str, Any]] | None:
    gallery = first(doc, 'div[data-gallery-name="product"]')
    if gallery is None:
        return None
    thumbnails = {img.get("data-name"): img.get("src") for img in gallery.cssselect('img[data-type="miniature"]')}
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    for img in gallery.cssselect('img[data-type="base"]'):
        name = img.get("data-name")
        key = name or img.get("src") or ""  # an image without a name is identified by its URL
        if key in seen:
            continue
        seen.add(key)
        images.append({"url": img.get("src"), "name": name, "thumbnail_url": thumbnails.get(name)})
    return images


def _master_sku(doc: HtmlElement) -> str | None:
    match = re.search(r"/p([^/?#]+)$", canonical_url(doc) or "")
    return unquote(match.group(1)) if match else None


def _family_name(doc: HtmlElement, master_sku: str | None) -> str | None:
    if not master_sku:
        return None
    breadcrumb = json_ld_of_type(doc, "BreadcrumbList") or {}
    for element in breadcrumb.get("itemListElement") or []:
        item = element.get("item")
        url = item.get("@id", "") if isinstance(item, dict) else str(item or "")
        if url.endswith("/p" + master_sku):
            return clean_text(element.get("name"))
    return None


def _category_path(layer: dict[str, Any]) -> list[tuple[str, str | None]]:
    page = layer.get("page") or {}
    path: list[tuple[str, str | None]] = []
    for i in range(1, 4):
        if not page.get(f"page_catid{i}"):
            break
        path.append((str(page[f"page_catid{i}"]), clean_text(page.get(f"page_cat{i}"))))
    return path
