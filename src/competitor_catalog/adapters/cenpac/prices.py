"""Cenpac price tiers, read from the degressive-price block of a variant web page.

Rules and their evidence: see docs/cenpac-observations.md (prices) and the adapter docstring.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from lxml.html import HtmlElement

from competitor_catalog.core.errors import PageParseError
from competitor_catalog.core.html import attr, first, json_attr, text_of
from competitor_catalog.core.normalize import clean_text, parse_decimal
from competitor_catalog.core.types import PriceBasis, PriceSnapshot, PriceTier, TaxMode


def _decimal(value: Any) -> Decimal | None:
    """Number from a config value: the site sends text ("2,57") and sometimes JSON numbers (20)."""
    return None if value is None else parse_decimal(str(value))


def parse_price_tiers(doc: HtmlElement, sku: str, default_currency: str) -> PriceSnapshot:
    warnings: list[str] = []

    cfg_el = next(
        (el for el in doc.cssselect("div[data-degressiveprices-config]") if el.get("data-degressiveprices") == sku),
        None,
    )
    cfg = json_attr(cfg_el, "data-degressiveprices-config")
    if not isinstance(cfg, dict):
        raise PageParseError(f"no price configuration for {sku}")

    offers = first(doc, 'div[itemprop="offers"]')
    if offers is None:
        raise PageParseError(f"no offers block for {sku}")
    price_el = first(offers, '[itemprop="price"]')
    sold_el = next(iter(offers.xpath("./span")), None)
    sold_by = text_of(sold_el)
    step = _order_step(sold_by or "")
    moq = _decimal(cfg.get("qty"))
    meta: dict[str, Any] = {"sold_by": sold_by, "min_order_quantity": _num(moq), "order_step": step}

    if price_el is None or price_el.get("content") is None:
        return PriceSnapshot([], False, reason=text_of(price_el) or "no price shown", metadata=meta)
    if not cfg.get("priceslist"):
        return PriceSnapshot([], False, reason="no price tiers on the page", metadata=meta)

    unit_el = first(offers, "span.whitespace-no-wrap")
    label = text_of(unit_el)
    if unit_el is None or label is None:
        raise PageParseError(f"no sales unit for {sku}")
    unit = re.sub(r"^(?:les|la|le|l['’])\s*", "", label, flags=re.I)  # "les" before "le"
    pieces_match = re.match(r"de\s+(\d[\d\s.,]*)", clean_text(unit_el.getparent().tail) or "")
    pieces = parse_decimal(pieces_match.group(1)) if pieces_match else None

    if re.search(r"\bHT\b", text_of(offers) or ""):
        tax = TaxMode.EXCL_TAX
    else:
        tax = TaxMode.UNKNOWN
        warnings.append("tax mode not shown")
    currency = attr(first(offers, '[itemprop="priceCurrency"]'), "content") or default_currency

    hbs = doc.xpath('//script[@id="HBS_degressiveprices"]')
    by_colis = bool(hbs) and bool(re.search(r"\{\{\s*this\.qty\s*\}\}\s*colis\s+et\s*\+", hbs[0].text or ""))
    multiplier, qunit = 1, unit
    if by_colis and unit != "colis":
        if step and step > 1:
            multiplier = step
        else:
            qunit = "colis"
            warnings.append("tiers counted in colis, colis size unknown")

    tiers: list[PriceTier] = []
    for item in cfg["priceslist"]:
        qty = _decimal(item.get("qty"))
        price = _decimal(item.get("price"))
        if qty is None or price is None:
            raise PageParseError(f"unparseable price tier {item!r}")
        if price <= 0:
            warnings.append(f"zero price tier dropped (qty {item.get('qty')})")
            continue
        old = _decimal(item.get("oldprice")) if item.get("oldprice") else None
        if item.get("oldprice") and old is None:
            warnings.append(f"unparseable struck price {item.get('oldprice')!r} ignored")
        tiers.append(
            PriceTier(
                price=price,
                currency=currency,
                price_basis=PriceBasis.UNIT,
                tax_mode=tax,
                min_quantity=qty * multiplier,
                quantity_unit=qunit,
                price_unit=unit,
                price_unit_quantity=Decimal(1),
                is_promotional=old is not None,
                metadata={
                    "site_quantity": item.get("qty"),
                    "site_quantity_unit": "colis" if by_colis else "sales_unit",
                    "discount": item.get("remise") or None,
                    "struck_price": str(old) if old is not None else None,
                    "units_per_price_unit": _num(pieces),
                    "min_order_quantity": _num(moq),
                    "order_step": step,
                },
            )
        )
    if not tiers:
        return PriceSnapshot([], False, reason="only zero-price tiers", metadata=meta, warnings=warnings)

    tiers.sort(key=lambda t: t.min_quantity or Decimal(0))
    unique: list[PriceTier] = []
    for tier in tiers:
        if unique and unique[-1].min_quantity == tier.min_quantity:
            warnings.append(f"duplicate price tier at {tier.min_quantity} dropped")
            continue
        unique.append(tier)
    for current, following in zip(unique, unique[1:], strict=False):
        current.max_quantity = following.min_quantity
    tiers = unique

    if qunit == unit and moq and moq > (tiers[0].min_quantity or Decimal(0)):
        if tiers[0].max_quantity is None or moq < tiers[0].max_quantity:
            tiers[0].min_quantity = moq
        else:
            warnings.append(f"minimum order quantity {moq} above the first tier band")

    head = parse_decimal(price_el.get("content"))
    if head is not None and head != min(t.price for t in tiers):
        warnings.append(f"headline price {head} is not the lowest tier price")

    return PriceSnapshot(
        tiers,
        True,
        metadata={
            **meta,
            "sales_unit": unit,
            "sales_unit_label": label,
            "units_per_sales_unit": _num(pieces),
            "tiers_by_colis": by_colis,
            "headline_price": str(head) if head is not None else None,
        },
        warnings=warnings,
    )


def _order_step(sold_by: str) -> int | None:
    """Order multiple from "vendu par colis de 20" (20) or "vendu à l'unité" (1)."""
    match = re.search(r"vendu par colis de\s+(\d+)", sold_by)
    if match:
        return int(match.group(1))
    if "unité" in sold_by:
        return 1
    return None


def _num(value: Decimal | None) -> int | str | None:
    """Integral decimals as int, other decimals as text (exact), None stays None."""
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return str(value)
