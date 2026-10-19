"""Shared, competitor-independent text/number/unit normalization helpers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

_SPACES = re.compile(r"[\s  ]+")
_NUMBER = re.compile(r"[-+]?\d[\d\s  .,]*")
_LABEL_UNIT = re.compile(r"^(?P<name>.*?)\s*\((?P<unit>[^()]+)\)\s*$")


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace; ``None`` stays ``None`` (missing), ``""`` stays ``""`` (empty)."""
    if value is None:
        return None
    return _SPACES.sub(" ", value).strip()


def fold(value: str) -> str:
    """Case/accent-insensitive comparison key: ``"Largeur intérieure (mm)" -> "largeur interieure (mm)"``."""
    decomposed = unicodedata.normalize("NFKD", value)
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _SPACES.sub(" ", no_accents.replace("’", "'")).strip().lower()


def slug_key(value: str) -> str:
    """Machine-friendly key: ``"Largeur (mm)" -> "largeur_mm"``."""
    folded = fold(value).replace("µ", "u").replace("²", "2")
    return re.sub(r"[^a-z0-9]+", "_", folded).strip("_")


def parse_decimal(value: str | None) -> Decimal | None:
    """Parse numbers written the French or the English way.

    ``"€ 1 234,56" -> 1234.56`` ; ``"0,81" -> 0.81`` ; ``"1280.0" -> 1280.0``.
    Returns ``None`` when no number is present.
    """
    if value is None:
        return None
    match = _NUMBER.search(value)
    if not match:
        return None
    raw = _SPACES.sub("", match.group(0)).rstrip(".,")
    if "," in raw and "." in raw:
        # The right-most separator is the decimal one.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".") if raw.count(",") == 1 else raw.replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def is_numeric_text(value: str) -> bool:
    return bool(re.fullmatch(r"\s*[-+]?\d+(?:[.,]\d+)?\s*", value))


def split_label_unit(label: str) -> tuple[str, str | None]:
    """``"Largeur (mm)" -> ("Largeur", "mm")`` ; ``"Couleur" -> ("Couleur", None)``."""
    match = _LABEL_UNIT.match(label)
    if not match:
        return label.strip(), None
    return match.group("name").strip(), match.group("unit").strip()


# unit (folded) -> (canonical unit, multiplier)
UNIT_CONVERSIONS: dict[str, tuple[str, Decimal]] = {
    "mm": ("mm", Decimal(1)),
    "cm": ("mm", Decimal(10)),
    "m": ("m", Decimal(1)),
    "km": ("m", Decimal(1000)),
    "u": ("um", Decimal(1)),
    "µ": ("um", Decimal(1)),
    "µm": ("um", Decimal(1)),
    "um": ("um", Decimal(1)),
    "micron": ("um", Decimal(1)),
    "microns": ("um", Decimal(1)),
    "g": ("g", Decimal(1)),
    "kg": ("g", Decimal(1000)),
    "g/m²": ("g_m2", Decimal(1)),
    "g/m2": ("g_m2", Decimal(1)),
    "gsm": ("g_m2", Decimal(1)),
    "l": ("l", Decimal(1)),
    "ml": ("l", Decimal("0.001")),
}

# canonical key suffix -> expected canonical unit
_KEY_SUFFIX_UNITS = {"_mm": "mm", "_m": "m", "_um": "um", "_g": "g", "_g_m2": "g_m2", "_l": "l"}
# Length conversions between mm and m are unambiguous.
_LENGTH_FACTORS = {("mm", "m"): Decimal("0.001"), ("m", "mm"): Decimal(1000)}


def convert_unit(value: Decimal, unit: str | None, target: str) -> Decimal | None:
    """Convert ``value`` expressed in ``unit`` into ``target`` canonical unit.

    Returns ``None`` when the conversion is not unambiguous (unknown unit, other dimension).
    """
    if unit is None:
        return None
    conv = UNIT_CONVERSIONS.get(unit.strip().lower()) or UNIT_CONVERSIONS.get(fold(unit))
    if conv is None:
        return None
    canonical, factor = conv
    value = value * factor
    if canonical == target:
        return value
    extra = _LENGTH_FACTORS.get((canonical, target))
    return value * extra if extra is not None else None


def to_json_number(value: Decimal) -> int | float:
    normalized = value.normalize()
    return int(normalized) if normalized == normalized.to_integral_value() else float(normalized)


def build_attribute(group: str | None, label: str, value: str | None) -> dict[str, Any]:
    """Structured representation of one characteristic, keeping the original label/value."""
    label = clean_text(label) or ""
    text = clean_text(value)
    name, unit = split_label_unit(label)
    item: dict[str, Any] = {
        "group": clean_text(group) if group else None,
        "label": label,
        "value": text,
        "key": slug_key(label),
        "name": name,
        "unit": unit,
    }
    if text is None or text == "":
        item["empty"] = True  # the site shows the characteristic with no value
    elif is_numeric_text(text):
        number = parse_decimal(text)
        if number is not None:
            item["numeric_value"] = to_json_number(number)
    return item


def build_normalized_attributes(
    attributes: Iterable[Mapping[str, Any]], mapping: Mapping[str, Iterable[str]]
) -> dict[str, Any]:
    """Derive canonical, comparable fields from raw characteristics.

    ``mapping`` maps a canonical key (``length_mm``, ``material``...) to the site labels
    that carry it. Keys ending with a unit suffix (``_mm``, ``_um``, ``_g_m2``...) are
    converted to that unit when the source unit is known; other keys keep the text value.
    Keys ending with ``_quantity``/``_count`` are parsed as plain numbers.
    Only the first matching label wins; values that cannot be converted are skipped
    (the raw characteristic is still available in ``attributes``).
    """
    by_label: dict[str, Mapping[str, Any]] = {}
    for attr in attributes:
        by_label.setdefault(fold(str(attr.get("label", ""))), attr)

    normalized: dict[str, Any] = {}
    for key, labels in mapping.items():
        for label in labels:
            attr = by_label.get(fold(label))
            if attr is None or attr.get("empty"):
                continue
            if key.endswith(("_quantity", "_count")):
                number = parse_decimal(str(attr.get("value")))
                if number is None:
                    continue
                normalized[key] = to_json_number(number)
                break
            target = next((u for s, u in _KEY_SUFFIX_UNITS.items() if key.endswith(s)), None)
            if target is None:
                normalized[key] = attr.get("value")
                break
            number = parse_decimal(str(attr.get("value"))) if attr.get("value") is not None else None
            if number is None:
                continue
            converted = convert_unit(number, attr.get("unit"), target)
            if converted is None:
                continue
            normalized[key] = to_json_number(converted)
            break
    return normalized
