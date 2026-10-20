"""Comparable dimensions of a product, read from its normalized attributes and its title.

A profile holds canonical measures (``box_mm`` = L x l x h, ``width_mm`` + ``length_m`` of a
roll...). Only values with an explicit unit are kept, so every measure is reliable; two
products are *comparable* when they share at least one measure.

Each shared measure scores 1 when the relative difference ``|a - b| / max(a, b)`` is within
``full_score_within``, 0 beyond ``zero_score_beyond`` (linear in between); a multi-value
measure takes its worst value (one wrong side = another box). The dimension score of a pair
is ``(mean + min) / 2`` of its measures, so a single mismatching measure weighs heavily.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from competitor_catalog.core.normalize import fold, parse_decimal

# key -> (label, unit, number of values)
MEASURES: dict[str, tuple[str, str, int]] = {
    "box_mm": ("Dimensions L x l x h", "mm", 3),
    "flat_mm": ("Format", "mm", 2),
    "width_mm": ("Largeur", "mm", 1),
    "length_m": ("Longueur", "m", 1),
    "thickness_um": ("Épaisseur", "µm", 1),
    "grammage_g_m2": ("Grammage", "g/m²", 1),
    "volume_l": ("Volume", "L", 1),
}
# Ordered comparison first; the same values in another order score at most this much.
UNORDERED_PENALTY = 0.9

_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"(mm|cm|m)"
_X = r"\s*[x×*]\s*"
_TRIPLE = re.compile(rf"{_NUM}\s*{_UNIT}?{_X}{_NUM}\s*{_UNIT}?{_X}{_NUM}\s*{_UNIT}(?![a-z0-9])")
_PAIR = re.compile(rf"{_NUM}\s*{_UNIT}?{_X}{_NUM}\s*{_UNIT}(?![a-z0-9])")
_THICKNESS = re.compile(rf"{_NUM}\s*(?:[µμ]m?|um|microns?)(?![a-z0-9])")
_GRAMMAGE = re.compile(rf"{_NUM}\s*g\s*/\s*m2(?![0-9])")
_VOLUME = re.compile(rf"{_NUM}\s*(?:l|litres?|liters?)(?![a-z0-9])")
_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0}


@dataclass(frozen=True)
class DimensionProfile:
    values: dict[str, tuple[float, ...]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # key -> "attributes" | "title"

    def __bool__(self) -> bool:
        return bool(self.values)

    def as_dict(self) -> dict[str, Any]:
        return {
            key: {"label": MEASURES[key][0], "unit": MEASURES[key][1], "value": list(v), "source": self.sources[key]}
            for key, v in self.values.items()
        }


def _num(text: str) -> float | None:
    value = parse_decimal(text)
    return float(value) if value is not None and value > 0 else None


def _attr(attributes: dict[str, Any], key: str) -> float | None:
    value = attributes.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


def parse_title(title: str) -> dict[str, tuple[float, ...]]:
    """Measures written with an explicit unit in a title ("300 x 200 x 150 mm", "50 mm x 66 m",
    "30,5 x 21,5 x 10,5 cm", "17 µ", "48 g/m²", "100 L")."""
    text = fold(title)
    found: dict[str, tuple[float, ...]] = {}
    triple = _TRIPLE.search(text)
    if triple:
        a, ua, b, ub, c, uc = triple.groups()
        numbers = [_num(a), _num(b), _num(c)]
        if all(numbers):
            units = [ua or uc, ub or uc, uc]
            found["box_mm"] = tuple(round(n * _TO_MM[u], 3) for n, u in zip(numbers, units, strict=True))
    else:
        pair = _PAIR.search(text)
        if pair:
            a, ua, b, ub = pair.groups()
            va, vb = _num(a), _num(b)
            if va and vb:
                if ub == "m" and ua in ("mm", "cm"):  # roll: width x length
                    found["width_mm"] = (round(va * _TO_MM[ua], 3),)
                    found["length_m"] = (vb,)
                elif ua is not None or ub != "m":
                    found["flat_mm"] = (round(va * _TO_MM[ua or ub], 3), round(vb * _TO_MM[ub], 3))
    for key, regex in (("thickness_um", _THICKNESS), ("grammage_g_m2", _GRAMMAGE), ("volume_l", _VOLUME)):
        match = regex.search(text)
        if match and _num(match.group(1)):
            found[key] = (_num(match.group(1)),)
    return found


def extract_profile(title: str | None, normalized_attributes: dict[str, Any] | None) -> DimensionProfile:
    """Normalized attributes first (structured data), then the title. Box dimensions: inner
    dimensions, else the title (which shows the inner dimensions on the sites seen so far),
    else length/width/height."""
    attrs = normalized_attributes or {}
    values: dict[str, tuple[float, ...]] = {}
    sources: dict[str, str] = {}

    def put(key: str, value: tuple[float, ...], source: str) -> None:
        if key not in values:
            values[key] = value
            sources[key] = source

    inner = [_attr(attrs, k) for k in ("inner_length_mm", "inner_width_mm", "inner_height_mm")]
    if all(inner):
        put("box_mm", tuple(inner), "attributes")
    width, roll_length = _attr(attrs, "width_mm"), _attr(attrs, "roll_length_m")
    if width and roll_length:
        put("width_mm", (width,), "attributes")
        put("length_m", (roll_length,), "attributes")
    thickness_um = _attr(attrs, "thickness_um")
    thickness_mm = _attr(attrs, "thickness_mm")
    if thickness_um or thickness_mm:
        put("thickness_um", (thickness_um or thickness_mm * 1000,), "attributes")
    grammage = _attr(attrs, "grammage_g_m2")
    if grammage:
        put("grammage_g_m2", (grammage,), "attributes")
    for key, value in parse_title(title or "").items():
        put(key, value, "title")
    outer = [_attr(attrs, k) for k in ("length_mm", "width_mm", "height_mm")]
    if all(outer):
        put("box_mm", tuple(outer), "attributes")
    return DimensionProfile(values, sources)


def measure_scores(rel_diff: np.ndarray, full: float, zero: float) -> np.ndarray:
    return np.clip(1.0 - (rel_diff - full) / (zero - full), 0.0, 1.0)


def _rel_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.abs(a - b) / np.maximum(a, b)


@dataclass
class DimensionScores:
    """Scores of one profile against every row of a :class:`DimensionIndex`."""

    total: np.ndarray  # (m,) NaN = not comparable
    per_key: dict[str, np.ndarray]  # key -> (m,) NaN = measure absent on that row

    def comparable(self, row: int) -> bool:
        return not np.isnan(self.total[row])


class DimensionIndex:
    def __init__(self, profiles: list[DimensionProfile], full: float, zero: float):
        self.profiles = profiles
        self.full, self.zero = full, zero
        m = len(profiles)
        self.arrays: dict[str, np.ndarray] = {}
        self.sorted_arrays: dict[str, np.ndarray] = {}
        for key, (_label, _unit, n) in MEASURES.items():
            array = np.full((m, n), np.nan)
            for i, profile in enumerate(profiles):
                value = profile.values.get(key)
                if value is not None and len(value) == n:
                    array[i] = value
            self.arrays[key] = array
            self.sorted_arrays[key] = -np.sort(-array, axis=1)
        self.has_dimensions = np.array([bool(p) for p in profiles], dtype=bool)

    def score(self, profile: DimensionProfile) -> DimensionScores:
        m = len(self.profiles)
        per_key: dict[str, np.ndarray] = {}
        for key, value in profile.values.items():
            array = self.arrays[key]
            query = np.array(value, dtype=float)
            if array.shape[1] != len(query):
                continue
            scores = measure_scores(_rel_diff(array, query), self.full, self.zero).min(axis=1)
            if len(query) > 1:
                swapped = measure_scores(
                    _rel_diff(self.sorted_arrays[key], -np.sort(-query)), self.full, self.zero
                ).min(axis=1)
                scores = np.fmax(scores, swapped * UNORDERED_PENALTY)
            per_key[key] = scores  # NaN where the row lacks the measure
        if not per_key:
            return DimensionScores(np.full(m, np.nan), per_key)
        stacked = np.vstack(list(per_key.values()))
        present = ~np.isnan(stacked)
        count = present.sum(axis=0)
        filled = np.where(present, stacked, 0.0)
        mean = np.divide(filled.sum(axis=0), count, out=np.zeros(m), where=count > 0)
        minimum = np.where(present, stacked, np.inf).min(axis=0)
        total = np.where(count > 0, (mean + minimum) / 2, np.nan)
        return DimensionScores(total, per_key)

    def details(self, profile: DimensionProfile, scores: DimensionScores, row: int) -> list[dict[str, Any]]:
        """Measure by measure comparison of ``profile`` with one row (for reviewers)."""
        other = self.profiles[row]
        result = []
        for key in MEASURES:
            mine, theirs = profile.values.get(key), other.values.get(key)
            if mine is None and theirs is None:
                continue
            label, unit, _n = MEASURES[key]
            item: dict[str, Any] = {
                "key": key,
                "label": label,
                "unit": unit,
                "competitor": list(mine) if mine else None,
                "reference": list(theirs) if theirs else None,
                "competitor_source": profile.sources.get(key),
                "reference_source": other.sources.get(key),
                "relative_difference": None,
                "score": None,
            }
            if mine and theirs and key in scores.per_key:
                diffs = _rel_diff(np.array(mine, dtype=float), np.array(theirs, dtype=float))
                item["relative_difference"] = round(float(diffs.max()), 4)
                item["score"] = round(float(scores.per_key[key][row]), 4)
            result.append(item)
        return result
