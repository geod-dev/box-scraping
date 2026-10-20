"""``config/matching.yaml``: reference catalog, embedding model, weights, thresholds, taxonomy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from competitor_catalog.core.normalize import fold

# Bump when the scoring code changes in a way that changes results: the next incremental run
# then recomputes every engine result (human decisions are kept).
ENGINE_VERSION = "2"
FEATURE_POLICIES = ("exclude", "review")


class MatchingConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EmbeddingSettings:
    provider: str = "voyage"
    model: str = "voyage-4"
    dimensions: int = 1024
    input_type: str | None = None
    batch_size: int = 128
    max_batch_tokens: int = 100_000
    max_description_chars: int = 1500
    # Account rate limits (Voyage: 3 RPM / 10K TPM without a payment method, 2000 RPM / 8M TPM
    # for voyage-4 with one). Requests are paced to stay within them.
    requests_per_minute: int = 2000
    tokens_per_minute: int = 8_000_000

    @property
    def batch_token_budget(self) -> int:
        """Estimated tokens per request: within the API limit and half the minute budget (a
        request bigger than the minute budget could never be accepted)."""
        return max(1, min(self.max_batch_tokens, self.tokens_per_minute // 2))


@dataclass(frozen=True)
class Feature:
    name: str
    policy: str  # exclude | review
    values: dict[str, tuple[str, ...]]  # value -> folded keywords
    types: frozenset[str] | None = None  # only read on products of these types


@dataclass(frozen=True)
class MatchingConfig:
    reference_competitor: str
    embedding: EmbeddingSettings
    semantic_top_k: int
    lexical_top_k: int
    dimension_top_k: int
    stored_candidates: int
    stored_excluded: int
    weights_with_dimensions: dict[str, float]
    weights_without_dimensions: dict[str, float]
    semantic_floor: float
    semantic_ceiling: float
    lexical_floor: float
    lexical_ceiling: float
    dimension_full_within: float
    dimension_zero_beyond: float
    auto_match_threshold: float
    min_gap: float
    review_threshold: float
    product_types: dict[str, tuple[str, ...]]  # type -> folded keywords
    compatible_types: frozenset[frozenset[str]]
    features: tuple[Feature, ...]
    require_comparable_dimensions: bool = True
    # Descriptions in the compared texts (embedding and TF-IDF). Off by default: the sites write
    # them too differently (Antalis: a spec line; Packdiscount: long SEO/FAQ prose), and they
    # drown the title. Characteristics are still read in the description either way.
    description_in_similarity: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def types_compatible(self, a: str | None, b: str | None) -> bool:
        return a is None or b is None or a == b or frozenset((a, b)) in self.compatible_types

    def config_hash(self, competitor_options: dict[str, Any] | None = None) -> str:
        """Identifies the scoring setup of one competitor: engine version, this file, and the
        competitor's own ``matching`` options (text cleaning)."""
        payload = {"engine": ENGINE_VERSION, "config": self.raw, "competitor": competitor_options or {}}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def public(self) -> dict[str, Any]:
        """What the API exposes (the whole file, plus the engine version)."""
        return {"engine_version": ENGINE_VERSION, **self.raw}


def _float(section: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(section.get(key, default))
    except (TypeError, ValueError):
        raise MatchingConfigError(f"{key} must be a number") from None


def _weights(data: Any, keys: tuple[str, ...], name: str) -> dict[str, float]:
    if not isinstance(data, dict) or set(data) != set(keys):
        raise MatchingConfigError(f"weights.{name} must define exactly {list(keys)}")
    weights = {k: float(data[k]) for k in keys}
    if any(w < 0 for w in weights.values()) or abs(sum(weights.values()) - 1) > 1e-6:
        raise MatchingConfigError(f"weights.{name} must be >= 0 and sum to 1")
    return weights


def _keywords(values: Any, where: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not values:
        raise MatchingConfigError(f"{where}: a non-empty list of keywords is expected")
    return tuple(fold(str(v)) for v in values)


def parse_matching_config(data: dict[str, Any]) -> MatchingConfig:
    if not data.get("reference_competitor"):
        raise MatchingConfigError("reference_competitor is required")
    emb = data.get("embedding") or {}
    embedding = EmbeddingSettings(
        provider=str(emb.get("provider", "voyage")),
        model=str(emb.get("model", "voyage-4")),
        dimensions=int(emb.get("dimensions", 1024)),
        input_type=emb.get("input_type"),
        batch_size=max(1, min(1000, int(emb.get("batch_size", 128)))),
        max_batch_tokens=int(emb.get("max_batch_tokens", 100_000)),
        max_description_chars=int(emb.get("max_description_chars", 1500)),
        requests_per_minute=max(1, int(emb.get("requests_per_minute", 2000))),
        tokens_per_minute=max(1, int(emb.get("tokens_per_minute", 8_000_000))),
    )
    if embedding.provider != "voyage":
        raise MatchingConfigError(f"unsupported embedding provider {embedding.provider!r}")
    if embedding.input_type not in (None, "query", "document"):
        raise MatchingConfigError("embedding.input_type must be null, query or document")
    retrieval = data.get("retrieval") or {}
    weights = data.get("weights") or {}
    norm = data.get("normalization") or {}
    semantic, lexical = norm.get("semantic") or {}, norm.get("lexical") or {}
    dims = data.get("dimensions") or {}
    thresholds = data.get("thresholds") or {}
    product_types = {
        str(key): _keywords(words, f"product_types.{key}") for key, words in (data.get("product_types") or {}).items()
    }
    compatible = set()
    for pair in data.get("compatible_types") or []:
        if not isinstance(pair, list) or len(pair) != 2 or not set(pair) <= set(product_types):
            raise MatchingConfigError(f"compatible_types: {pair!r} must be a pair of known product types")
        compatible.add(frozenset(pair))
    features = []
    for name, spec in (data.get("features") or {}).items():
        policy = (spec or {}).get("policy")
        if policy not in FEATURE_POLICIES:
            raise MatchingConfigError(f"features.{name}.policy must be one of {FEATURE_POLICIES}")
        values = {str(v): _keywords(words, f"features.{name}.{v}") for v, words in (spec.get("values") or {}).items()}
        if not values:
            raise MatchingConfigError(f"features.{name}: no values")
        types = spec.get("types")
        if types is not None and not set(types) <= set(product_types):
            raise MatchingConfigError(
                f"features.{name}.types: unknown product types {sorted(set(types) - set(product_types))}"
            )
        features.append(Feature(str(name), policy, values, frozenset(types) if types else None))
    config = MatchingConfig(
        reference_competitor=str(data["reference_competitor"]),
        embedding=embedding,
        semantic_top_k=int(retrieval.get("semantic_top_k", 25)),
        lexical_top_k=int(retrieval.get("lexical_top_k", 25)),
        dimension_top_k=int(retrieval.get("dimension_top_k", 15)),
        stored_candidates=int(retrieval.get("stored_candidates", 10)),
        stored_excluded=int(retrieval.get("stored_excluded", 5)),
        weights_with_dimensions=_weights(
            weights.get("with_dimensions", {"semantic": 0.5, "lexical": 0.2, "dimensions": 0.3}),
            ("semantic", "lexical", "dimensions"),
            "with_dimensions",
        ),
        weights_without_dimensions=_weights(
            weights.get("without_dimensions", {"semantic": 0.75, "lexical": 0.25}),
            ("semantic", "lexical"),
            "without_dimensions",
        ),
        semantic_floor=_float(semantic, "floor", 0.55),
        semantic_ceiling=_float(semantic, "ceiling", 0.95),
        lexical_floor=_float(lexical, "floor", 0.0),
        lexical_ceiling=_float(lexical, "ceiling", 0.6),
        dimension_full_within=_float(dims, "full_score_within", 0.02),
        dimension_zero_beyond=_float(dims, "zero_score_beyond", 0.20),
        auto_match_threshold=_float(thresholds, "auto_match", 90),
        min_gap=_float(thresholds, "min_gap", 8),
        review_threshold=_float(thresholds, "review", 75),
        product_types=product_types,
        compatible_types=frozenset(compatible),
        features=tuple(features),
        require_comparable_dimensions=bool(data.get("require_comparable_dimensions", True)),
        description_in_similarity=bool((data.get("text") or {}).get("description_in_similarity", False)),
        raw=data,
    )
    if not config.semantic_floor < config.semantic_ceiling or not config.lexical_floor < config.lexical_ceiling:
        raise MatchingConfigError("normalization: floor must be lower than ceiling")
    if not config.dimension_full_within < config.dimension_zero_beyond:
        raise MatchingConfigError("dimensions: full_score_within must be lower than zero_score_beyond")
    if not 0 <= config.review_threshold <= config.auto_match_threshold <= 100:
        raise MatchingConfigError("thresholds: expected 0 <= review <= auto_match <= 100")
    return config


def load_matching_config(path: Path) -> MatchingConfig:
    if not path.is_file():
        raise MatchingConfigError(f"matching config not found: {path}")
    with path.open(encoding="utf-8") as fh:
        return parse_matching_config(yaml.safe_load(fh) or {})
