"""Antalis France e-shop adapter (https://www.antalis.fr/eshop/...).

Observed site structure (see docs/antalis-observations.md for details/evidence):

* Every e-shop page embeds ``<script id="dataLayerScript" contextDataLayer="..." pageDataModel="{json}">``.
  ``contextDataLayer`` gives the page type, ``pageDataModel.category_type_id`` the real
  hierarchy node id (``HQ02953``); the sector root uses the placeholder ``HQ00000``.
* Page types: ``SECTOR`` (e.g. ``/cat-3``) and ``SUB_CATEGORY`` (``sub-hq...``) show cards of
  child nodes; ``PRODUCT_LISTING`` (``plp-hq...``) shows product-family cards;
  ``PRODUCT_DETAILS`` (``pdp-hq...``) is a product *family* listing its SKUs (20 per page,
  ``?no=20`` pagination); ``SKU_DETAILS`` (``.../sku-503623``) is the product page.
* The breadcrumb (schema.org BreadcrumbList) gives the navigation hierarchy.
* SKU pages carry a schema.org ``Product`` JSON-LD (sku, name, description, images), the
  characteristics table (``div.sku-attributes``) and prices (``div[data-price-bloc]``, and a
  ``scale-block`` with quantity bands for degressive prices).

Only the HTML structure is used: URL patterns are never trusted as identifiers.
"""

from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from lxml.html import HtmlElement

from competitor_catalog.core.adapter import CompetitorAdapter
from competitor_catalog.core.errors import PageParseError, UnexpectedPageError
from competitor_catalog.core.html import (
    attr,
    block_text,
    canonical_url,
    first,
    json_attr,
    json_ld_of_type,
    parse_html,
    text_of,
)
from competitor_catalog.core.normalize import (
    build_attribute,
    build_normalized_attributes,
    clean_text,
    parse_decimal,
)
from competitor_catalog.core.types import (
    CategoryData,
    CategoryRef,
    ListingPage,
    PageKind,
    PriceBasis,
    PriceSnapshot,
    PriceTier,
    ProductData,
    ProductLink,
    ProductPricePage,
    TaxMode,
)
from competitor_catalog.http.base import FetchedPage

LISTING_PAGE_TYPES = {
    "SECTOR": "sector",
    "SUB_CATEGORY": "sub_category",
    "PRODUCT_LISTING": "product_listing",
    "PRODUCT_DETAILS": "product_family",
}
PRODUCT_PAGE_TYPE = "SKU_DETAILS"
_EXTERNAL_ID = re.compile(r"^HQ\d+$")
_SKU_REF = re.compile(r"^\d{3,}$")
_IMAGE_VARIANT = re.compile(r"^(?P<prefix>LO[A-Z]+)_(?P<rest>.+)$")
_VARIANT_RANGE = re.compile(r"\[\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\]")


class AntalisAdapter(CompetitorAdapter):
    # ------------------------------------------------------------------ helpers
    @property
    def _ignored_external_ids(self) -> set[str]:
        return set(self.config.options.get("ignored_category_external_ids", ["HQ00000"]))

    def _doc(self, page: FetchedPage) -> HtmlElement:
        try:
            return parse_html(page.text)
        except Exception as exc:  # lxml raises ParserError on empty documents
            raise UnexpectedPageError(f"unparseable HTML: {exc}", url=page.requested_url) from exc

    @staticmethod
    def _context(doc: HtmlElement, page: FetchedPage) -> tuple[str, dict[str, Any]]:
        script = first(doc, "script#dataLayerScript")
        page_type = attr(script, "contextDataLayer")
        if not page_type:
            raise UnexpectedPageError(
                "no Antalis page context (dataLayerScript) found: blocked, error or non e-shop page",
                url=page.requested_url,
            )
        model = json_attr(script, "pageDataModel") or {}
        return page_type, model

    def _page_url(self, doc: HtmlElement, page: FetchedPage) -> str:
        return self.normalize_url(canonical_url(doc) or page.final_url)

    def _breadcrumb(self, doc: HtmlElement) -> tuple[list[CategoryRef], str | None]:
        """Ancestors (root first, without the home link) and the active item label."""
        nav = first(doc, "nav.eshop-breadcrumb")
        if nav is None:
            return [], None
        ancestors: list[CategoryRef] = []
        active: str | None = None
        for li in nav.cssselect("li.e-shop-breadcrumb-item"):
            name_el = first(li, '[itemprop="name"]')
            name = text_of(name_el if name_el is not None else li)
            link = first(li, "a[href]")
            if link is None:
                active = name
                continue
            href = link.get("href", "")
            if href.rstrip("/").endswith("/eshop/home"):
                continue
            ancestors.append(CategoryRef(url=self.normalize_url(href), name=name))
        return ancestors, active

    def _external_id(self, model: dict[str, Any]) -> str | None:
        value = str(model.get("category_type_id") or "").strip().upper()
        if _EXTERNAL_ID.match(value) and value not in self._ignored_external_ids:
            return value
        return None

    def _links(self, elements: list[HtmlElement]) -> list[str]:
        urls: list[str] = []
        for el in elements:
            href = (el.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            url = self.normalize_url(href)
            if "/eshop/" not in url or "/eshop/ws/" in url or url.endswith("/eshop/home"):
                continue
            if self.config.host not in url:
                continue
            if url not in urls:
                urls.append(url)
        return urls

    # ------------------------------------------------------------------ contract
    def classify(self, page: FetchedPage) -> PageKind:
        page_type, _ = self._context(self._doc(page), page)
        if page_type == PRODUCT_PAGE_TYPE:
            return PageKind.PRODUCT
        if page_type in LISTING_PAGE_TYPES:
            return PageKind.LISTING
        raise UnexpectedPageError(f"unsupported Antalis page type {page_type!r}", url=page.requested_url)

    def parse_listing(self, page: FetchedPage) -> ListingPage:
        doc = self._doc(page)
        page_type, model = self._context(doc, page)
        kind = LISTING_PAGE_TYPES.get(page_type)
        if kind is None:
            raise UnexpectedPageError(f"not a listing page ({page_type})", url=page.requested_url)

        url = self._page_url(doc, page)
        ancestors, active = self._breadcrumb(doc)
        h1 = text_of(first(doc, "h1"))
        name = active or h1
        if not name:
            raise PageParseError("category without name (no breadcrumb, no h1)", url=url)
        results = _to_int(model.get("results_number"))
        category = CategoryData(
            url=url,
            name=name,
            kind=kind,
            external_id=self._external_id(model),
            ancestors=ancestors,
            title=h1,
            extra={
                "page_type": page_type,
                "results_number": results,
                "seo_title": clean_text(model.get("title")),
                "category_type_id_raw": model.get("category_type_id"),
            },
        )
        listing = ListingPage(url=self.normalize_url(page.final_url), category=category)

        if kind in ("sector", "sub_category"):
            cards = [
                card
                for card in doc.cssselect("div.eshop-card.eshop-card-sector")
                if "card-sleeknote-placeholder" not in (card.get("class") or "")
            ]
            links: list[HtmlElement] = []
            for card in cards:
                links.extend(card.cssselect("a[href]"))
            listing.child_category_urls = self._links(links)
            if not listing.child_category_urls:
                listing.warnings.append("navigation page without child cards")
        elif kind == "product_listing":
            listing.child_category_urls = self._links(doc.cssselect("div.card-group a.card[href]"))
            listing.expected_item_count = results
            if results and not listing.child_category_urls:
                raise PageParseError(f"listing announces {results} results but no card was found", url=url)
        else:  # product_family
            for card in doc.cssselect('div#skuLineForm div.card[id^="skuline_"]'):
                link = next(iter(self._links(card.cssselect('a[href*="/sku-"]'))), None)
                reference = card.get("id", "").removeprefix("skuline_") or None
                if link:
                    listing.product_links.append(ProductLink(url=link, reference=reference))
                else:
                    listing.warnings.append(f"SKU card {reference} without product link")
            listing.expected_item_count = results
            if results and not listing.product_links:
                raise PageParseError(f"family announces {results} SKUs but no SKU card was found", url=url)

        next_link = first(doc, "li.eshop-pagination__page--next a[href]")
        if next_link is not None:
            next_url = self.normalize_url(next_link.get("href"))
            if next_url != listing.url:
                listing.next_page_url = next_url
        return listing

    def parse_product(self, page: FetchedPage) -> ProductData:
        doc = self._doc(page)
        page_type, model = self._context(doc, page)
        if page_type != PRODUCT_PAGE_TYPE:
            raise UnexpectedPageError(f"not a product page ({page_type})", url=page.requested_url)
        url = self._page_url(doc, page)
        ld = json_ld_of_type(doc, "Product") or {}
        warnings: list[str] = []
        reference = self._reference(doc, ld, warnings)
        if reference is None:
            raise PageParseError("product reference (SKU) not found", url=url)

        ancestors, _ = self._breadcrumb(doc)
        family = ancestors[-1] if ancestors else None
        category = (
            CategoryRef(url=family.url, name=family.name, external_id=self._external_id(model)) if family else None
        )
        if category is None:
            warnings.append("no breadcrumb: category association unknown")

        title_el = first(doc, "h1.prod-title")
        title = text_of(title_el) or clean_text(ld.get("name"))
        description = text_of(first(doc, "div.skudetails_container-grid p.es-lh-18")) or clean_text(
            ld.get("description")
        )
        long_description = self._long_description(doc)

        attributes = self._attributes(doc, warnings)
        mapping = self.config.options.get("attribute_mapping") or {}
        normalized = build_normalized_attributes(attributes, mapping) if attributes is not None else None

        prices = self._prices(doc, ld)
        warnings.extend(prices.warnings)
        actions = first(doc, "[data-salesUnits]")
        brand = ld.get("brand", {}).get("name") if isinstance(ld.get("brand"), dict) else ld.get("brand")
        extra: dict[str, Any] = {
            "page_type": page_type,
            "seo_title": clean_text(model.get("title")),
            "sales_units": json_attr(actions, "data-salesUnits"),
            "price_unit": json_attr(actions, "data-priceUnit"),
            "weight_text": text_of(first(doc, "[data-price-bloc] [data-weight]")),
            "documents": self._documents(doc),
            "availability": prices.metadata.get("availability"),
            "price_display": prices.metadata.get("price_display", "displayed" if prices.price_found else None),
        }
        return ProductData(
            url=url,
            catalog_reference=reference,
            external_id=reference,
            title=title,
            description=description,
            long_description=long_description,
            brand=clean_text(brand) if brand else None,
            attributes=attributes,
            normalized_attributes=normalized,
            images=self._images(doc, ld),
            category=category,
            breadcrumb=ancestors,
            extra=extra,
            prices=prices,
            warnings=warnings,
        )

    def parse_prices(self, page: FetchedPage) -> ProductPricePage:
        doc = self._doc(page)
        page_type, _ = self._context(doc, page)
        if page_type != PRODUCT_PAGE_TYPE:
            raise UnexpectedPageError(f"not a product page ({page_type})", url=page.requested_url)
        ld = json_ld_of_type(doc, "Product") or {}
        warnings: list[str] = []
        reference = self._reference(doc, ld, warnings)
        prices = self._prices(doc, ld)
        prices.warnings.extend(warnings)
        return ProductPricePage(url=self._page_url(doc, page), catalog_reference=reference, prices=prices)

    # ------------------------------------------------------------------ product parts
    def _reference(self, doc: HtmlElement, ld: dict[str, Any], warnings: list[str]) -> str | None:
        candidates = {
            "json_ld": clean_text(str(ld["sku"])) if ld.get("sku") else None,
            "breadcrumb": self._breadcrumb(doc)[1],
            "add_to_cart": attr(first(doc, "[add-to-cart-global][productId]"), "productId"),
        }
        values = [v for v in candidates.values() if v and _SKU_REF.match(v)]
        if not values:
            return None
        if len(set(values)) > 1:
            warnings.append(f"inconsistent SKU references on page: {candidates}")
        return values[0]

    @staticmethod
    def _long_description(doc: HtmlElement) -> str | None:
        """Marketing text of the "Informations Produits" tab, without the pictogram column."""
        tab = first(doc, "#inpageNavigationItem-01")
        if tab is None:
            return None
        tab = copy.deepcopy(tab)
        for picto in tab.cssselect("a.btn-picto"):
            column = next((a for a in picto.iterancestors("div") if "col-" in (a.get("class") or "")), None)
            if column is not None and column.getparent() is not None:
                column.getparent().remove(column)
        return block_text(tab)

    def _attributes(self, doc: HtmlElement, warnings: list[str]) -> list[dict[str, Any]] | None:
        container = first(doc, "div.sku-attributes")
        attributes: list[dict[str, Any]] = []
        if container is None:
            warnings.append("characteristics table (div.sku-attributes) not found")
        else:
            for item in container.cssselect("div.sku-attributes-item"):
                group = text_of(first(item, "h3"))
                for li in item.cssselect("li"):
                    spans = li.findall("span")
                    if not spans:
                        continue
                    label = text_of(spans[0])
                    value = text_of(spans[1]) if len(spans) > 1 else ""
                    if label:
                        attributes.append(build_attribute(group, label, value) | {"source": "characteristics"})

        # "Informations techniques" tab: <p><span class="es-f-exbold">Label :</span></p><p>value</p>
        tech = first(doc, "#inpageNavigationItem-02")
        if tech is not None:
            label: str | None = None
            values: list[str] = []

            def flush() -> None:
                if label:
                    attributes.append(
                        build_attribute("Informations techniques", label, "\n".join(values))
                        | {"source": "technical_information"}
                    )

            for p in tech.cssselect("p"):
                strong = first(p, "span.es-f-exbold")
                if strong is not None:
                    flush()
                    label = (text_of(strong) or "").rstrip(" :")
                    values = []
                    rest = (text_of(p) or "").removeprefix(text_of(strong) or "").strip()
                    if rest:
                        values.append(rest)
                elif label and (text := text_of(p)):
                    values.append(text)
            flush()

        # Pictograms (environmental certificates, labels...) are characteristics too.
        for picto in doc.cssselect("a.btn-picto"):
            heading = picto.xpath(
                "preceding::span[contains(concat(' ', normalize-space(@class), ' '), ' es-f-exbold ')][1]"
            )
            group = text_of(heading[0]) if heading else "Pictogrammes"
            description = attr(picto, "data-description")
            if description:
                attributes.append(
                    build_attribute("Pictogrammes", group or "Pictogramme", description)
                    | {
                        "source": "pictogram",
                        "code": attr(picto, "data-code"),
                        "attribute_code": attr(picto, "data-attrcode"),
                    }
                )
        if container is None:
            # The main table is absent: the characteristic list is unknown, not partial.
            return None
        return attributes

    def _images(self, doc: HtmlElement, ld: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Group size variants (LOSQS_/LOSQL_ prefixes) of the same picture; only URLs seen on the page.

        ``None`` when the page has neither a gallery nor JSON-LD images (not observed)."""
        gallery = first(doc, "div.eshop-vertical-gallery")
        sources: list[tuple[str, str | None]] = []
        for img in doc.cssselect("div.eshop-vertical-gallery img[src]"):
            sources.append((img.get("src"), img.get("alt")))
        ld_images = ld.get("image")
        if ld_images is None and gallery is None:
            return None
        for item in ld_images if isinstance(ld_images, list) else [ld_images]:
            if isinstance(item, dict):  # schema.org ImageObject
                item = item.get("contentUrl") or item.get("url")
            if isinstance(item, str) and item.strip():
                sources.append((item, None))

        images: dict[str, dict[str, Any]] = {}
        for src, alt in sources:
            url = self.normalize_url(src)
            filename = url.rsplit("/", 1)[-1]
            match = _IMAGE_VARIANT.match(filename)
            key, variant = (match.group("rest"), match.group("prefix")) if match else (filename, "original")
            entry = images.setdefault(key, {"position": len(images), "variants": {}, "alt": None})
            entry["variants"].setdefault(variant, url)
            entry["alt"] = entry["alt"] or clean_text(alt)
        result = []
        for entry in images.values():
            variants = entry["variants"]
            # Prefer the largest rendition observed for the main URL.
            for preferred in ("LOSQXL", "LOSQL", "LOSQM", "original", "LOSQS"):
                if preferred in variants:
                    entry["url"] = variants[preferred]
                    break
            else:
                entry["url"] = next(iter(variants.values()))
            result.append(
                {"url": entry["url"], "position": entry["position"], "alt": entry["alt"], "variants": variants}
            )
        return result

    def _documents(self, doc: HtmlElement) -> list[dict[str, Any]] | None:
        tab = first(doc, "#inpageNavigationItem-03")
        if tab is None:
            return None  # not observed (never erases documents already stored)
        return [{"title": text_of(a), "url": self.normalize_url(a.get("href"))} for a in tab.cssselect("a[href]")]

    # ------------------------------------------------------------------ prices
    def _undisplayed_price(self, doc: HtmlElement, offers: dict[str, Any], currency: str) -> PriceSnapshot:
        """No price shown to the visitor (e.g. machines: "Demander à être rappelé").

        The JSON-LD offer may still expose a price. It is recorded only if the adapter option
        ``record_undisplayed_json_ld_price`` is enabled, flagged as not displayed, with unknown
        basis and tax mode because the page does not state them.
        """
        contact = first(doc, '.eshop-product-card__actions a[onclick*="openContactForm"]')
        metadata: dict[str, Any] = {
            "price_display": "on_request" if contact is not None else "not_displayed",
            "page_action": text_of(contact),
            "ld_availability": offers.get("availability"),
        }
        ld_price = parse_decimal(str(offers["price"])) if offers.get("price") is not None else None
        if ld_price is None or not self.config.options.get("record_undisplayed_json_ld_price", True):
            reason = "price not displayed on the page" + (" (price on request)" if contact is not None else "")
            return PriceSnapshot(tiers=[], price_found=False, reason=reason, metadata=metadata)
        tier = PriceTier(
            price=ld_price,
            currency=str(offers.get("priceCurrency") or currency),
            price_basis=PriceBasis.UNKNOWN,
            tax_mode=TaxMode.UNKNOWN,
            is_promotional=None,
            metadata={
                "tier_source": "json_ld_offer",
                "displayed": False,
                "note": "price only present in schema.org JSON-LD; on products with a displayed price this "
                "value equals the displayed excl.-tax price per price unit (first tier), not verified here",
            },
        )
        return PriceSnapshot(
            tiers=[tier],
            price_found=True,
            metadata=metadata,
            warnings=["price not displayed on the page; value taken from the JSON-LD offer (basis/tax unknown)"],
        )

    def _prices(self, doc: HtmlElement, ld: dict[str, Any]) -> PriceSnapshot:
        # The page also contains empty `data-price-bloc` templates (add-to-cart modal): only a
        # block that actually carries a price is the product's price block.
        bloc = next((b for b in doc.cssselect("div[data-price-bloc]") if first(b, "[data-price]") is not None), None)
        container = first(doc, "div.skudetails_container-grid")
        offers = ld.get("offers") if isinstance(ld.get("offers"), dict) else {}
        currency = attr(container, "data-currency") or offers.get("priceCurrency") or self.config.default_currency
        metadata: dict[str, Any] = {}
        if bloc is None:
            return self._undisplayed_price(doc, offers, currency)

        labels = [text_of(el) or "" for el in bloc.cssselect("div.es-f-exbold") if attr(el, "data-price") is None]
        tax_mode = TaxMode.UNKNOWN
        if any("H.T" in label or "HT" in label.split() for label in labels):
            tax_mode = TaxMode.EXCL_TAX
        elif any("TTC" in label for label in labels):
            tax_mode = TaxMode.INCL_TAX
        is_promo = any("promo" in label.lower() for label in labels)
        main = first(bloc, "[data-price]")
        main_price = parse_decimal(attr(main, "data-price"))
        unit_text = next(
            (text_of(el) for el in bloc.cssselect("div.es-fs-12") if (text_of(el) or "").startswith("/")), None
        )
        strike = first(bloc, "span.es-td-strike")
        reduction = strike.getnext() if strike is not None else None
        price_unit_info = json_attr(first(doc, "[data-priceUnit]"), "data-priceUnit")
        if not isinstance(price_unit_info, dict):
            price_unit_info = {}
        unit_quantity, unit_label = _unit_from_text(unit_text)
        price_unit_label = unit_label or clean_text(price_unit_info.get("desc"))
        metadata.update(
            {
                "availability": text_of(first(bloc, ".eshop-product-stock")),
                "price_label": " / ".join(label for label in labels if label),
                "unit_text": unit_text,
                "vat_rate": attr(bloc, "data-vat-rate"),
                "page_promotion": is_promo,
                "promotion_title": text_of(first(doc, ".promotion-panel-head h4")),
            }
        )
        if strike is not None:
            strike_price = parse_decimal(text_of(strike))
            metadata["strikethrough_price"] = str(strike_price) if strike_price is not None else None
            metadata["strikethrough_reduction"] = text_of(reduction)
            metadata["strikethrough_kind"] = (
                "scale_reduction" if first(bloc, "[data-scale-reduction-rate-label]") is not None else "list_price"
            )
        if main_price is None:
            return PriceSnapshot(
                tiers=[], price_found=False, reason="price block without price value", metadata=metadata
            )

        warnings: list[str] = []

        common = {
            "currency": currency,
            "price_basis": PriceBasis.UNIT,
            "tax_mode": tax_mode,
            "quantity_unit": price_unit_label,
            "price_unit": price_unit_label,
        }
        if unit_text and unit_quantity is None:
            warnings.append(f"price unit text {unit_text!r} not understood")
        tiers: list[PriceTier] = []
        scale_items = doc.cssselect("div.scale-block div.slider-scroll div.item[data-variant]")
        for item in scale_items:
            price_el = first(item, "[data-price]")
            price = parse_decimal(attr(price_el, "data-price"))
            bounds = _VARIANT_RANGE.search(item.get("data-variant") or "")
            try:
                low = Decimal(bounds.group(1)) if bounds else None
                high = Decimal(bounds.group(2)) if bounds and bounds.group(2) else None
            except InvalidOperation:
                low = None
            if price is None or low is None:
                # Never fall back to the headline price or store a partial set of tiers:
                # the whole capture is rejected and reported as a parse failure.
                raise PageParseError(f"unparseable price tier {item.get('data-variant')!r}")
            tier_unit_text = next(
                (text_of(el) for el in item.cssselect("div.es-fs-12") if (text_of(el) or "").startswith("/")), None
            )
            tier_unit_quantity, tier_unit_label = _unit_from_text(tier_unit_text)
            if tier_unit_text and tier_unit_quantity is None:
                raise PageParseError(f"unparseable tier price unit {tier_unit_text!r}")
            label_spans = first(item, "div.es-f-bold")
            tier_label = text_of(label_spans)
            unit_span = first(item, "span[data-unitcode]")
            tiers.append(
                PriceTier(
                    price=price,
                    min_quantity=low,
                    # Bands are contiguous ([36,108], [108,180], [180]) and the label of the
                    # second band ("3 boites" = 108 units) shows 108 belongs to it: upper
                    # bounds are exclusive.
                    max_quantity=high,
                    max_inclusive=False,
                    is_promotional=None if is_promo else False,
                    price_unit_quantity=tier_unit_quantity if tier_unit_text else unit_quantity,
                    metadata={
                        "tier_source": "price_scale",
                        "data_variant": item.get("data-variant"),
                        "tier_label": tier_label,
                        "tier_label_unit_code": attr(unit_span, "data-unitcode"),
                        "quantity_price": attr(price_el, "data-quantity-price"),
                        "base_unit_quantity_price": attr(price_el, "data-base-unit-quantity-price"),
                        "real_price": attr(price_el, "data-real-price"),
                        "price_unit_code": price_unit_info.get("unitCode"),
                        "unit_text": tier_unit_text,
                    },
                    **{**common, "price_unit": tier_unit_label or common["price_unit"]},
                )
            )
        if not tiers and first(doc, "div.scale-block") is not None:
            warnings.append("price scale block present but without tiers")
        if not tiers:
            moq = (
                parse_decimal(str(price_unit_info.get("minOrderQty")))
                if price_unit_info.get("minOrderQty") is not None
                else None
            )
            tier_meta: dict[str, Any] = {
                "tier_source": "single_price",
                "min_quantity_source": "price unit minimum order quantity" if moq is not None else None,
                "quantity_price": attr(main, "data-quantity-price"),
                "base_unit_quantity_price": attr(main, "data-base-unit-quantity-price"),
                "real_price": attr(main, "data-real-price"),
                "price_unit_code": price_unit_info.get("unitCode"),
            }
            if strike is not None:
                tier_meta["strikethrough_price"] = metadata["strikethrough_price"]
                tier_meta["discount"] = metadata["strikethrough_reduction"]
            tiers.append(
                PriceTier(
                    price=main_price,
                    min_quantity=moq,
                    max_quantity=None,
                    is_promotional=is_promo,
                    price_unit_quantity=unit_quantity,
                    metadata=tier_meta,
                    **common,
                )
            )
        elif min(t.price for t in tiers) != main_price:
            warnings.append(f"headline price {main_price} is not the lowest tier price")
        # JSON-LD offer price = first tier price on tiered products, the price otherwise.
        ld_price = parse_decimal(str(offers["price"])) if offers.get("price") is not None else None
        if ld_price is not None and ld_price not in {t.price for t in tiers}:
            warnings.append(f"JSON-LD offer price {ld_price} matches no displayed price")
        return PriceSnapshot(tiers=tiers, price_found=True, metadata=metadata, warnings=warnings)


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _unit_from_text(text: str | None) -> tuple[Decimal | None, str | None]:
    """``"/ 1 unité" -> (1, "unité")`` ; ``"/ 1 000 feuille(s)" -> (1000, "feuille(s)")``."""
    if not text:
        return None, None
    match = re.match(r"^/\s*(\d[\d\s\u00a0\u202f.,]*)?\s*(\D.*)$", text)
    if not match:
        return None, None
    quantity = parse_decimal(match.group(1)) if match.group(1) else Decimal(1)
    return quantity, clean_text(match.group(2))
