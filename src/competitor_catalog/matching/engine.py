"""Scoring and decision (pure: no database, no network).

For one competitor product:

1. **Retrieval** - the reference catalog is scored with three cheap vectorized signals
   (embedding cosine, TF-IDF cosine, dimensions) and the union of the top candidates of
   each signal forms the pool, so a candidate missed by one signal is caught by another.
2. **Re-ranking** - each pooled candidate gets its sub-scores, its final score
   ``100 * sum(weight * sub-score)`` (weights depend on whether the dimensions are
   comparable), its differences, its exclusions (certain incompatibilities) and its review
   flags (conflicts that forbid an automatic association). A shared measure that differs
   (another size) is an exclusion by default.
3. **Decision** - ``auto_matched`` when the best candidate reaches the auto threshold, leads
   the second one by the minimum gap and has no review flag; a best candidate reaching the
   ``certain`` threshold is automatic even within the gap (with a warning reason) as long as
   the second one stays below it; ``needs_review`` from the review threshold; ``no_match``
   below.

The score is a similarity score on a 0-100 scale, not a probability of equivalence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from competitor_catalog.matching.config import MatchingConfig
from competitor_catalog.matching.dimensions import DimensionIndex, DimensionProfile, DimensionScores, extract_profile
from competitor_catalog.matching.features import ProductFeatures, compare_features, extract_features
from competitor_catalog.matching.lexical import LexicalIndex
from competitor_catalog.matching.text import (
    ProductSource,
    TextOptions,
    effective_description,
    embedding_text,
    lexical_text,
    product_hash,
    tokens,
)


@dataclass(frozen=True)
class ProductDoc:
    """Everything the engine reads of one product, precomputed once per run."""

    product_id: int
    competitor_id: int
    status: str
    title: str
    reference: str | None
    url: str | None
    category: str | None
    description: str | None
    embedding_text: str
    tokens: tuple[str, ...]
    profile: DimensionProfile
    features: ProductFeatures
    product_hash: str

    def summary(self) -> dict[str, Any]:
        """What the engine understood of the product (shown to reviewers)."""
        return {
            "title": self.title,
            "description_used": self.description,
            "category": self.category,
            "dimensions": self.profile.as_dict(),
            **self.features.as_dict(),
        }


def build_doc(source: ProductSource, options: TextOptions, config: MatchingConfig) -> ProductDoc:
    description = effective_description(source, options)
    return ProductDoc(
        product_id=source.product_id,
        competitor_id=source.competitor_id,
        status=source.status,
        title=source.title or "",
        reference=source.reference,
        url=source.url,
        category=source.category,
        description=description,
        embedding_text=embedding_text(source, options),
        tokens=tuple(tokens(lexical_text(source, options))),
        profile=extract_profile(source.title, source.normalized_attributes),
        features=extract_features(source.title, description, source.category, source.normalized_attributes, config),
        product_hash=product_hash(source),
    )


class ReferenceIndex:
    """The reference catalog (Antalis), indexed for the three retrieval signals."""

    def __init__(self, docs: list[ProductDoc], vectors: np.ndarray, config: MatchingConfig):
        if len(docs) != len(vectors):
            raise ValueError("one vector per reference product is required")
        self.docs = docs
        self.position = {doc.product_id: i for i, doc in enumerate(docs)}
        self.vectors = np.asarray(vectors, dtype=np.float32).reshape(len(docs), -1) if docs else np.zeros((0, 1))
        self.lexical = LexicalIndex([list(doc.tokens) for doc in docs])
        self.dimensions = DimensionIndex([doc.profile for doc in docs], config.dimension_tolerance)
        digest = hashlib.sha256()
        for doc in sorted(docs, key=lambda d: d.product_id):
            digest.update(f"{doc.product_id}:{doc.product_hash};".encode())
        self.catalog_hash = digest.hexdigest()[:16]

    def __len__(self) -> int:
        return len(self.docs)


@dataclass
class Candidate:
    product_id: int
    score: float
    semantic: float | None  # normalized 0..1 (None = no embedding available)
    lexical: float
    dimensions: float | None  # None = not comparable
    excluded: bool
    exclusions: list[dict[str, Any]]
    review_flags: list[dict[str, Any]]
    details: dict[str, Any]
    rank: int | None = None


@dataclass
class MatchOutcome:
    status: str  # auto_matched | needs_review | no_match
    best: Candidate | None
    runner_up_score: float | None
    gap: float | None
    reasons: list[dict[str, Any]]
    candidates: list[Candidate] = field(default_factory=list)  # to store: ranked, then excluded

    @property
    def score(self) -> float | None:
        return self.best.score if self.best else None


def _scale(value: float, floor: float, ceiling: float) -> float:
    return float(min(1.0, max(0.0, (value - floor) / (ceiling - floor))))


def _top(values: np.ndarray, k: int, *, positive: bool = False) -> list[int]:
    if k <= 0 or len(values) == 0:
        return []
    valid = ~np.isnan(values)
    if positive:
        valid &= values > 0
    indexes = np.flatnonzero(valid)
    if len(indexes) <= k:
        return indexes.tolist()
    chosen = np.argpartition(-values[indexes], k - 1)[:k]
    return indexes[chosen].tolist()


class MatchEngine:
    def __init__(self, config: MatchingConfig, index: ReferenceIndex):
        self.config = config
        self.index = index

    # ------------------------------------------------------------------ retrieval
    def _signals(
        self, doc: ProductDoc, vector: np.ndarray | None
    ) -> tuple[np.ndarray | None, np.ndarray, DimensionScores]:
        semantic = self.index.vectors @ np.asarray(vector, dtype=np.float32) if vector is not None else None
        lexical = self.index.lexical.scores(list(doc.tokens))
        dimensions = self.index.dimensions.score(doc.profile)
        return semantic, lexical, dimensions

    def match(
        self, doc: ProductDoc, vector: np.ndarray | None, *, rejected_ids: frozenset[int] = frozenset()
    ) -> MatchOutcome:
        if len(self.index) == 0:
            return MatchOutcome(
                "no_match", None, None, None, [_reason("empty_reference", "the reference catalog is empty")]
            )
        semantic, lexical, dimensions = self._signals(doc, vector)
        cfg = self.config
        pool: dict[int, list[str]] = {}  # row -> signals that retrieved it
        # Many reference products can share the same size: among them, the closest texts first.
        same_size = dimensions.total + 0.01 * (semantic if semantic is not None else lexical)
        tops = [
            ("semantic", _top(semantic, cfg.semantic_top_k) if semantic is not None else []),
            ("lexical", _top(lexical, cfg.lexical_top_k, positive=True)),
            ("dimensions", _top(np.where(dimensions.total > 0, same_size, np.nan), cfg.dimension_top_k)),
        ]
        for signal, rows in tops:
            for row in rows:
                pool.setdefault(row, []).append(signal)
        candidates = [
            self._candidate(doc, row, semantic, lexical, dimensions, rejected_ids, retrieved_by=signals)
            for row, signals in pool.items()
        ]
        return self._decide(candidates)

    def compare(self, doc: ProductDoc, vector: np.ndarray | None, reference_id: int) -> Candidate:
        """Score one chosen reference product (manual review: "why not this one?")."""
        row = self.index.position[reference_id]
        semantic, lexical, dimensions = self._signals(doc, vector)
        return self._candidate(doc, row, semantic, lexical, dimensions, frozenset(), retrieved_by=["manual"])

    # ------------------------------------------------------------------ re-ranking
    def _candidate(
        self,
        doc: ProductDoc,
        row: int,
        semantic: np.ndarray | None,
        lexical: np.ndarray,
        dimensions: DimensionScores,
        rejected_ids: frozenset[int],
        *,
        retrieved_by: list[str],
    ) -> Candidate:
        cfg = self.config
        ref = self.index.docs[row]
        semantic_raw = float(semantic[row]) if semantic is not None else None
        semantic_score = (
            _scale(semantic_raw, cfg.semantic_floor, cfg.semantic_ceiling) if semantic_raw is not None else None
        )
        lexical_raw = float(lexical[row])
        lexical_score = _scale(lexical_raw, cfg.lexical_floor, cfg.lexical_ceiling)
        comparable = dimensions.comparable(row)
        dimension_score = float(dimensions.total[row]) if comparable else None

        weights = dict(cfg.weights_with_dimensions if comparable else cfg.weights_without_dimensions)
        if semantic_score is None:  # no embedding (manual comparison only): renormalize the others
            weights.pop("semantic")
            total = sum(weights.values())
            weights = {k: v / total for k, v in weights.items()}
        parts = {"semantic": semantic_score, "lexical": lexical_score, "dimensions": dimension_score}
        score = round(100 * sum(w * (parts[k] or 0.0) for k, w in weights.items()), 2)

        features = compare_features(doc.features, ref.features, cfg)
        exclusions = list(features.exclusions)
        if ref.product_id in rejected_ids:
            exclusions.append(
                {"code": "rejected_by_human", "message": "a reviewer rejected this product for this competitor product"}
            )
        review_flags = list(features.review_flags)
        dimension_details = self.index.dimensions.details(doc.profile, dimensions, row)
        if comparable and dimensions.mismatch(row):
            measures = [d for d in dimension_details if d["score"] == 0]
            mismatch = {
                "code": "dimension_mismatch",
                "field": ",".join(d["key"] for d in measures),
                "message": "different dimensions: "
                + "; ".join(
                    f"{d['label']} {_values(d['competitor'])} vs {_values(d['reference'])} {d['unit']}"
                    for d in measures
                ),
            }
            (exclusions if cfg.dimension_mismatch_policy == "exclude" else review_flags).append(mismatch)
        either_has_dimensions = bool(doc.profile) or bool(ref.profile)
        if cfg.require_comparable_dimensions and either_has_dimensions and not comparable:
            review_flags.append(
                {
                    "code": "dimensions_not_comparable",
                    "message": "dimensions known on "
                    + ("both sides but not on the same measures" if doc.profile and ref.profile else "one side only"),
                }
            )
        differences = [
            {
                "kind": "dimension",
                "field": d["key"],
                "label": d["label"],
                "competitor": d["competitor"],
                "reference": d["reference"],
                "unit": d["unit"],
                "relative_difference": d["relative_difference"],
            }
            for d in dimension_details
            if d["score"] is not None and d["score"] < 1
        ]
        differences += [
            {"kind": "feature", "field": c["key"], "competitor": c["competitor"], "reference": c["reference"]}
            for c in features.compared
            if c["status"] == "conflict"
        ]
        details = {
            "reference": {"title": ref.title, "reference": ref.reference, "category": ref.category},
            "weights": {k: round(v, 4) for k, v in weights.items()},
            "semantic": {"cosine": _round(semantic_raw), "score": _round(semantic_score)},
            "lexical": {
                "cosine": round(lexical_raw, 4),
                "score": round(lexical_score, 4),
                "shared_terms": self.index.lexical.shared_terms(list(doc.tokens), row),
            },
            "dimensions": {"comparable": comparable, "score": _round(dimension_score), "measures": dimension_details},
            "features": features.compared,
            "differences": differences,
            "exclusions": exclusions,
            "review_flags": review_flags,
            "retrieved_by": retrieved_by,
        }
        return Candidate(
            product_id=ref.product_id,
            score=score,
            semantic=semantic_score,
            lexical=lexical_score,
            dimensions=dimension_score,
            excluded=bool(exclusions),
            exclusions=exclusions,
            review_flags=review_flags,
            details=details,
        )

    # ------------------------------------------------------------------ decision
    def _decide(self, candidates: list[Candidate]) -> MatchOutcome:
        cfg = self.config
        ranked = sorted((c for c in candidates if not c.excluded), key=_rank_key)
        excluded = sorted((c for c in candidates if c.excluded), key=_rank_key)
        for rank, candidate in enumerate(ranked, 1):
            candidate.rank = rank
        stored = ranked[: cfg.stored_candidates] + excluded[: cfg.stored_excluded]
        if not ranked:
            reasons = [_reason("no_compatible_candidate", "no candidate left after excluding incompatible products")]
            return MatchOutcome("no_match", None, None, None, reasons, stored)
        best = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        runner_up = second.score if second else None
        gap = round(best.score - runner_up, 2) if runner_up is not None else None
        reasons: list[dict[str, Any]] = []
        if best.score >= cfg.auto_match_threshold:
            reasons.append(_reason("score_above_auto", f"score {best.score:g} >= {cfg.auto_match_threshold:g}"))
            blockers = []
            if gap is not None and gap < cfg.min_gap and best.score >= cfg.certain_threshold > runner_up:
                reasons.append(
                    _reason(
                        "close_runner_up",
                        f"warning: second candidate only {gap:g} points behind (score {runner_up:g}), "
                        f"associated anyway: score {best.score:g} >= {cfg.certain_threshold:g} (identical)",
                        warning=True,
                    )
                )
            elif gap is not None and gap < cfg.min_gap:
                blockers.append(
                    _reason(
                        "ambiguous_gap",
                        f"second candidate only {gap:g} points behind (< {cfg.min_gap:g}): "
                        + (
                            f"both score >= {cfg.certain_threshold:g}, nothing tells them apart"
                            if runner_up >= cfg.certain_threshold
                            else "similar, not clearly equivalent"
                        ),
                    )
                )
            elif gap is not None:
                reasons.append(_reason("clear_gap", f"{gap:g} points ahead of the second candidate"))
            else:
                reasons.append(_reason("single_candidate", "no other compatible candidate"))
            blockers += [_reason(flag["code"], flag["message"]) for flag in best.review_flags]
            reasons += blockers
            status = "needs_review" if blockers else "auto_matched"
        elif best.score >= cfg.review_threshold:
            reasons.append(
                _reason(
                    "review_band",
                    f"score {best.score:g} between {cfg.review_threshold:g} and {cfg.auto_match_threshold:g}",
                )
            )
            reasons += [_reason(flag["code"], flag["message"]) for flag in best.review_flags]
            status = "needs_review"
        else:
            reasons.append(_reason("score_below_review", f"best score {best.score:g} < {cfg.review_threshold:g}"))
            status = "no_match"
        return MatchOutcome(status, best, runner_up, gap, reasons, stored)


def _reason(code: str, message: str, *, warning: bool = False) -> dict[str, Any]:
    return {"code": code, "message": message, **({"warning": True} if warning else {})}


def _values(values: list[float] | None) -> str:
    return " × ".join(f"{v:g}" for v in values or [])


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _rank_key(c: Candidate) -> tuple:
    """Score first. Ties (sub-scores saturated at their ceiling, e.g. two titles that only
    differ by a color) go to the candidate without review flags, then to the closest raw
    cosines, so a "blanc" tape does not rank the brown one first by product id."""
    semantic = c.details["semantic"]["cosine"]
    return (-c.score, len(c.review_flags), -(semantic or 0.0), -c.details["lexical"]["cosine"], c.product_id)
