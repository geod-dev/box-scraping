"""Product type and categorical characteristics (keyword rules of ``config/matching.yaml``).

Used to exclude candidates with a *certain* incompatibility (product types that are not
compatible, ``exclude`` features) and to send ambiguous ones to human review (``review``
features). Unknown values never exclude anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from competitor_catalog.core.normalize import fold
from competitor_catalog.matching.config import MatchingConfig
from competitor_catalog.matching.text import find_keyword


@dataclass(frozen=True)
class ProductFeatures:
    product_type: str | None = None
    type_source: str | None = None  # "title" | "category"
    values: dict[str, frozenset[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_type": self.product_type,
            "product_type_source": self.type_source,
            "features": {k: sorted(v) for k, v in self.values.items()},
        }


def detect_type(text: str | None, config: MatchingConfig) -> str | None:
    """The type whose keyword appears first (French product names start with their head noun:
    "Machine à fermer les caisses", "Ruban adhésif pour caisse"); longest keyword on a tie."""
    if not text:
        return None
    folded = fold(text)
    best: tuple[int, int, str] | None = None
    for type_key, keywords in config.product_types.items():
        for keyword in keywords:
            position = find_keyword(folded, keyword)
            if position is not None and (best is None or (position, -len(keyword)) < (best[0], best[1])):
                best = (position, -len(keyword), type_key)
    return best[2] if best else None


def extract_features(
    title: str | None,
    description: str | None,
    category: str | None,
    normalized_attributes: dict[str, Any] | None,
    config: MatchingConfig,
) -> ProductFeatures:
    product_type, source = detect_type(title, config), "title"
    if product_type is None:
        product_type, source = detect_type(category, config), "category"
    if product_type is None:
        source = None
    attribute_text = " ".join(str(v) for v in (normalized_attributes or {}).values() if isinstance(v, str))
    folded = fold(" ".join(t for t in (title, description, category, attribute_text) if t))
    values: dict[str, frozenset[str]] = {}
    for feature in config.features:
        if feature.types is not None and product_type not in feature.types:
            continue
        found = {
            value
            for value, keywords in feature.values.items()
            if any(find_keyword(folded, keyword) is not None for keyword in keywords)
        }
        if found:
            values[feature.name] = frozenset(found)
    return ProductFeatures(product_type, source, values)


@dataclass(frozen=True)
class FeatureComparison:
    types_compatible: bool
    exclusions: list[dict[str, Any]]  # certain incompatibilities
    review_flags: list[dict[str, Any]]  # conflicts that block an automatic association
    compared: list[dict[str, Any]]  # every characteristic seen on either side


def compare_features(mine: ProductFeatures, theirs: ProductFeatures, config: MatchingConfig) -> FeatureComparison:
    compatible = config.types_compatible(mine.product_type, theirs.product_type)
    exclusions: list[dict[str, Any]] = []
    review_flags: list[dict[str, Any]] = []
    compared: list[dict[str, Any]] = [
        {
            "key": "product_type",
            "competitor": mine.product_type,
            "reference": theirs.product_type,
            "status": "unknown"
            if not (mine.product_type and theirs.product_type)
            else "match"
            if compatible
            else "conflict",
        }
    ]
    if not compatible:
        exclusions.append(
            {
                "code": "incompatible_product_type",
                "field": "product_type",
                "competitor": mine.product_type,
                "reference": theirs.product_type,
                "message": f"incompatible product types: {mine.product_type} vs {theirs.product_type}",
            }
        )
    for feature in config.features:
        a, b = mine.values.get(feature.name), theirs.values.get(feature.name)
        if not a and not b:
            continue
        status = "unknown" if not (a and b) else "match" if a & b else "conflict"
        compared.append(
            {
                "key": feature.name,
                "competitor": sorted(a) if a else None,
                "reference": sorted(b) if b else None,
                "status": status,
                "policy": feature.policy,
            }
        )
        if status != "conflict":
            continue
        entry = {
            "code": "feature_conflict",
            "field": feature.name,
            "competitor": sorted(a),
            "reference": sorted(b),
            "message": f"{feature.name}: {', '.join(sorted(a))} vs {', '.join(sorted(b))}",
        }
        (exclusions if feature.policy == "exclude" else review_flags).append(entry)
    return FeatureComparison(compatible, exclusions, review_flags, compared)
