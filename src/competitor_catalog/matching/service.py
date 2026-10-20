"""Product matching, database side: eligibility, change detection, runs, human decisions.

Data model (see README "Product matching"):

* ``product_matches`` - exactly one row per competitor product: its current status, the
  associated / proposed reference product, scores, the hashes it was computed from, the
  human decision and the deprecation details. Reruns update this row, never duplicate it.
* ``product_match_candidates`` - the ranked candidates of the latest computation, with the
  score of each criterion (what the review screen shows).
* ``product_match_events`` - append-only history: every computation that changed the result,
  every human decision, every deprecation, with a snapshot of the previous state.

Change detection: a match stores the hash of its competitor product and of its reference
product. When either changed (or the reference product disappeared), the match becomes
``deprecated`` (status kept in ``previous_status``, a notification is created). Deprecated
matches are recomputed by the next incremental run, or alone with ``mode=deprecated``.
Human decisions (``validated``/``rejected``) are never recomputed unless deprecated.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.db.models import (
    MATCH_ENGINE_STATUSES,
    MATCH_HUMAN_STATUSES,
    Category,
    Competitor,
    Product,
    ProductEmbedding,
    ProductMatch,
    ProductMatchCandidate,
    ProductMatchEvent,
)
from competitor_catalog.db.session import transaction
from competitor_catalog.matching.config import MatchingConfig
from competitor_catalog.matching.embeddings import Embedder, EmbeddingCache, EmbeddingStats, load_cached
from competitor_catalog.matching.engine import MatchEngine, MatchOutcome, ProductDoc, ReferenceIndex, build_doc
from competitor_catalog.matching.text import ProductSource, TextOptions, product_hash
from competitor_catalog.services.notifications import notify
from competitor_catalog.services.runs import NULL_OBSERVER, RunObserver, utcnow

log = logging.getLogger(__name__)

MODES = ("incremental", "deprecated", "all")
DEPRECATION_REASONS = {
    "product_changed": "the competitor product changed (title, description, category or characteristics)",
    "antalis_product_changed": "the associated Antalis product changed",
    "antalis_product_missing": "the associated Antalis product is no longer in the catalog (missing)",
    "antalis_product_deleted": "the associated Antalis product was deleted",
    "reset_requested": "a reviewer asked to recompute this match",
}
REFUSALS = {
    "matching_reference": "the reference catalog is not matched against itself",
    "reference_missing": "the reference competitor is not in the database",
    "matching_not_configured": "VOYAGE_API_KEY is not set: matching is disabled",
    "catalog_required": "the competitor needs a successful full catalog sync (products parsed) before matching",
    "reference_catalog_required": "the reference catalog needs a successful full catalog sync before matching",
    "competitor_disabled": "the competitor is disabled in its config",
}
WRITE_BATCH = 200
ENGINE_ACTOR = "engine"


class MatchingError(Exception):
    """A matching request that must be refused; ``code`` is stable and documented."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or REFUSALS.get(code, code))
        self.code = code
        self.message = message or REFUSALS.get(code, code)


@dataclass(frozen=True)
class MatchingRuntime:
    """Matching setup shared by the API and the worker."""

    config: MatchingConfig
    embedder_factory: Callable[[], Embedder] | None = None  # None = no API key: matching disabled

    @property
    def configured(self) -> bool:
        return self.embedder_factory is not None


@dataclass
class MatchingOptions:
    mode: str = "incremental"  # incremental | deprecated | all

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {list(MODES)}")


# --------------------------------------------------------------------------- eligibility


def reference_competitor(session: Session, config: MatchingConfig) -> Competitor | None:
    return session.scalar(select(Competitor).where(Competitor.key == config.reference_competitor))


def refusal(session: Session, competitor: Competitor, runtime: MatchingRuntime | None) -> str | None:
    """None when the competitor can be matched now, else the refusal code."""
    if runtime is None or not runtime.configured:
        return "matching_not_configured"
    if competitor.key == runtime.config.reference_competitor:
        return "matching_reference"
    if not competitor.enabled:
        return "competitor_disabled"
    reference = reference_competitor(session, runtime.config)
    if reference is None:
        return "reference_missing"
    if competitor.last_full_catalog_at is None:
        return "catalog_required"
    if reference.last_full_catalog_at is None:
        return "reference_catalog_required"
    return None


def eligible_products(competitor_id: int):
    """Products the engine can read: active, with parsed details and a title."""
    return select(Product.id).where(
        Product.competitor_id == competitor_id,
        Product.status == "active",
        Product.title.is_not(None),
        Product.details_updated_at.is_not(None),
    )


# --------------------------------------------------------------------------- loading


def load_sources(session: Session, *conditions: Any) -> list[ProductSource]:
    rows = session.execute(
        select(
            Product.id,
            Product.competitor_id,
            Product.status,
            Product.title,
            Product.description,
            Product.long_description,
            Category.name,
            Product.normalized_attributes,
            Product.catalog_reference,
            Product.url,
        )
        .outerjoin(Category, Category.id == Product.category_id)
        .where(*conditions)
        .order_by(Product.id)
    )
    return [ProductSource(*row) for row in rows]


def sources_by_id(session: Session, ids: Iterable[int]) -> dict[int, ProductSource]:
    ids = sorted(set(ids))
    result: dict[int, ProductSource] = {}
    for start in range(0, len(ids), 2000):
        for source in load_sources(session, Product.id.in_(ids[start : start + 2000])):
            result[source.product_id] = source
    return result


def catalog_hash(sources: Iterable[ProductSource]) -> str:
    digest = hashlib.sha256()
    for source in sorted(sources, key=lambda s: s.product_id):
        digest.update(f"{source.product_id}:{product_hash(source)};".encode())
    return digest.hexdigest()[:16]


def text_options(config: MatchingConfig, competitor_options: dict[str, Any] | None) -> TextOptions:
    return TextOptions.from_options(competitor_options, config.embedding.max_description_chars)


# --------------------------------------------------------------------------- history


def _snapshot(match: ProductMatch, session: Session, top: int = 3) -> dict[str, Any]:
    """The state of a match before a change, kept in its history."""
    candidates = session.execute(
        select(ProductMatchCandidate.antalis_product_id, ProductMatchCandidate.score)
        .where(ProductMatchCandidate.match_id == match.id, ProductMatchCandidate.excluded.is_(False))
        .order_by(ProductMatchCandidate.rank)
        .limit(top)
    ).all()
    return {
        "status": match.status,
        "antalis_product_id": match.antalis_product_id,
        "proposed_antalis_product_id": match.proposed_antalis_product_id,
        "score": _num(match.score),
        "runner_up_score": _num(match.runner_up_score),
        "decided_by": match.decided_by,
        "decided_at": match.decided_at.isoformat() if match.decided_at else None,
        "decision_comment": match.decision_comment,
        "manual_choice": match.manual_choice,
        "previous_status": match.previous_status,
        "deprecated_reason": match.deprecated_reason,
        "computed_at": match.computed_at.isoformat() if match.computed_at else None,
        "top_candidates": [{"antalis_product_id": pid, "score": _num(score)} for pid, score in candidates],
    }


def _num(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


def _event(
    session: Session,
    match: ProductMatch,
    event: str,
    *,
    from_status: str | None,
    from_antalis: int | None,
    actor: str,
    reason: str | None = None,
    job_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    session.add(
        ProductMatchEvent(
            match_id=match.id,
            product_id=match.product_id,
            competitor_id=match.competitor_id,
            event=event,
            from_status=from_status,
            to_status=match.status,
            from_antalis_product_id=from_antalis,
            to_antalis_product_id=match.antalis_product_id or match.proposed_antalis_product_id,
            score=match.score,
            actor=actor,
            reason=reason,
            job_id=job_id,
            data=data or {},
        )
    )


# --------------------------------------------------------------------------- change detection


@dataclass
class Deprecation:
    match_id: int
    product_id: int
    competitor_id: int
    title: str | None
    previous_status: str
    reason: str
    antalis_product_id: int | None


def _deprecate(
    session: Session, match: ProductMatch, reason: str, *, actor: str = ENGINE_ACTOR, job_id: int | None = None
) -> None:
    snapshot = _snapshot(match, session)
    previous = match.status
    from_antalis = match.antalis_product_id or match.proposed_antalis_product_id
    match.previous_status = previous
    match.status = "deprecated"
    match.deprecated_at = utcnow()
    match.deprecated_reason = reason
    match.revision += 1
    _event(
        session,
        match,
        "reset" if reason == "reset_requested" else "deprecated",
        from_status=previous,
        from_antalis=from_antalis,
        actor=actor,
        reason=DEPRECATION_REASONS.get(reason, reason),
        job_id=job_id,
        data=snapshot,
    )


def detect_stale_matches(
    session: Session,
    competitor_ids: Iterable[int] | None = None,
    *,
    product_ids: Iterable[int] | None = None,
    job_id: int | None = None,
) -> list[Deprecation]:
    """Deprecate the current matches whose competitor product or reference product changed.

    ``competitor_ids`` / ``product_ids``: only the matches involving these competitors'
    products / these products (as the matched product or as the reference one); neither =
    every match. Creates one ``matches_deprecated`` notification per matched competitor.
    """
    stmt = select(ProductMatch).where(ProductMatch.status != "deprecated").with_for_update(skip_locked=True)
    if competitor_ids is not None:
        ids = list(competitor_ids)
        of_competitors = select(Product.id).where(Product.competitor_id.in_(ids))
        stmt = stmt.where(
            or_(
                ProductMatch.competitor_id.in_(ids),
                ProductMatch.antalis_product_id.in_(of_competitors),
                ProductMatch.proposed_antalis_product_id.in_(of_competitors),
            )
        )
    if product_ids is not None:
        products = list(product_ids)
        stmt = stmt.where(
            or_(
                ProductMatch.product_id.in_(products),
                ProductMatch.antalis_product_id.in_(products),
                ProductMatch.proposed_antalis_product_id.in_(products),
            )
        )
    matches = list(session.scalars(stmt))
    if not matches:
        return []
    targets = {m.id: watched_target(m) for m in matches}
    sources = sources_by_id(session, [m.product_id for m in matches] + [t for t in targets.values() if t])
    deprecations: list[Deprecation] = []
    for match in matches:
        reason = _stale_reason(match, targets[match.id], sources)
        if reason is None:
            continue
        deprecations.append(
            Deprecation(
                match.id,
                match.product_id,
                match.competitor_id,
                sources[match.product_id].title if match.product_id in sources else None,
                match.status,
                reason,
                targets[match.id],
            )
        )
        _deprecate(session, match, reason, job_id=job_id)
    session.flush()
    notify_deprecations(session, deprecations, job_id=job_id)
    return deprecations


def watched_target(match: ProductMatch) -> int | None:
    """The reference product a match depends on: the association, or the proposal under review.
    (The closest candidate of a no_match / rejected match is informative only.)"""
    if match.status in ("auto_matched", "validated"):
        return match.antalis_product_id
    if match.status == "needs_review":
        return match.proposed_antalis_product_id
    return None


def _stale_reason(match: ProductMatch, target_id: int | None, sources: dict[int, ProductSource]) -> str | None:
    source = sources.get(match.product_id)
    if source is not None and match.product_hash and product_hash(source) != match.product_hash:
        return "product_changed"
    if target_id is None:
        # the match depended on a reference product (its hash is stored) that was deleted
        if match.antalis_product_hash and match.status in ("auto_matched", "validated", "needs_review"):
            return "antalis_product_deleted"
        return None
    target = sources.get(target_id)
    if target is None:
        return "antalis_product_deleted"
    if target.status != "active":
        return "antalis_product_missing"
    if match.antalis_product_hash and product_hash(target) != match.antalis_product_hash:
        return "antalis_product_changed"
    return None


def notify_deprecations(session: Session, deprecations: list[Deprecation], *, job_id: int | None = None) -> None:
    by_competitor: dict[int, list[Deprecation]] = {}
    for item in deprecations:
        by_competitor.setdefault(item.competitor_id, []).append(item)
    for competitor_id, items in by_competitor.items():
        competitor = session.get(Competitor, competitor_id)
        name = competitor.name if competitor else f"competitor #{competitor_id}"
        key = competitor.key if competitor else str(competitor_id)
        reasons = Counter(i.reason for i in items)
        previous = Counter(i.previous_status for i in items)
        human = sum(previous[s] for s in MATCH_HUMAN_STATUSES)
        if len(items) == 1:
            title = f"Matching invalidated: {items[0].title or f'product #{items[0].product_id}'} ({name})"
        else:
            title = f"{len(items)} matchings invalidated: {name}"
        message = (
            "; ".join(f"{n} x {DEPRECATION_REASONS[r]}" for r, n in reasons.most_common())
            + f". Previous statuses: {', '.join(f'{n} {s}' for s, n in previous.most_common())}"
            + (f" (including {human} human decision(s))" if human else "")
            + f". They are listed by GET /api/v1/matching/deprecated?competitor={key} and recomputed by "
            f"POST /api/v1/matching/competitors/{key}/run with mode=deprecated."
        )
        notify(
            session,
            level="warning",
            kind="matches_deprecated",
            title=title,
            message=message,
            competitor_id=competitor_id,
            job_id=job_id,
            data={
                "count": len(items),
                "by_reason": dict(reasons),
                "by_previous_status": dict(previous),
                "matches": [
                    {
                        "match_id": i.match_id,
                        "product_id": i.product_id,
                        "title": i.title,
                        "previous_status": i.previous_status,
                        "reason": i.reason,
                        "antalis_product_id": i.antalis_product_id,
                    }
                    for i in items[:50]
                ],
            },
        )


# --------------------------------------------------------------------------- run


@dataclass
class MatchingResult:
    status: str
    counters: dict[str, Any] = field(default_factory=dict)


class MatchingRun:
    """One matching run of one competitor against the reference catalog."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        competitor_id: int,
        runtime: MatchingRuntime,
        options: MatchingOptions,
        observer: RunObserver = NULL_OBSERVER,
        *,
        job_id: int | None = None,
        competitor_options: dict[str, Any] | None = None,
    ):
        self.factory = factory
        self.competitor_id = competitor_id
        self.runtime = runtime
        self.config = runtime.config
        self.options = options
        self.observer = observer
        self.job_id = job_id
        self.competitor_options = competitor_options or {}
        self.counters: Counter[str] = Counter()
        self.embedding_stats = EmbeddingStats()

    def _report(self, step: str, done: int, goal: int | None = None) -> None:
        self.observer.checkpoint({**self.counters, **self.embedding_stats.as_dict()}, 0)
        self.observer.progress(step, done, goal)

    def run(self) -> MatchingResult:
        self._report("detect_changes", 0)
        with transaction(self.factory) as session:
            competitor = session.get(Competitor, self.competitor_id)
            code = refusal(session, competitor, self.runtime)
            if code:
                raise MatchingError(code)
            reference = reference_competitor(session, self.config)
            name, reference_id = competitor.name, reference.id
            deprecations = detect_stale_matches(session, [self.competitor_id], job_id=self.job_id)
            self.counters["deprecated_detected"] = len(deprecations)
        self._report("detect_changes", 1, 1)
        self.observer.check_stop()

        competitor_text = text_options(self.config, self.competitor_options)
        reference_text = text_options(self.config, None)
        config_hash = self.config.config_hash(self.competitor_options)
        with transaction(self.factory) as session:
            reference_sources = load_sources(session, Product.id.in_(eligible_products(reference_id)))
            competitor_sources = load_sources(session, Product.id.in_(eligible_products(self.competitor_id)))
            current = {
                m.product_id: (m.status, m.config_hash, m.reference_catalog_hash, m.revision)
                for m in session.scalars(select(ProductMatch).where(ProductMatch.competitor_id == self.competitor_id))
            }
        ref_hash = catalog_hash(reference_sources)
        selected = [s for s in competitor_sources if self._selected(current.get(s.product_id), config_hash, ref_hash)]
        self.counters["eligible_products"] = len(competitor_sources)
        self.counters["reference_products"] = len(reference_sources)
        self.counters["selected"] = len(selected)
        log.info(
            "matching %s (%s): %d/%d products selected, %d reference products",
            name,
            self.options.mode,
            len(selected),
            len(competitor_sources),
            len(reference_sources),
        )
        if selected:
            self._match(selected, reference_sources, competitor_text, reference_text, current, config_hash, ref_hash)
        self._report("finalize", 0, 1)
        with transaction(self.factory) as session:
            self._notify_summary(session, name)
        self._report("finalize", 1, 1)
        return MatchingResult("succeeded", {**self.counters, **self.embedding_stats.as_dict()})

    def _selected(
        self, current: tuple[str, str | None, str | None, int] | None, config_hash: str, ref_hash: str
    ) -> bool:
        if current is None:
            return self.options.mode != "deprecated"
        status, row_config, row_catalog, _revision = current
        if status == "deprecated":
            return True
        if self.options.mode == "deprecated" or status in MATCH_HUMAN_STATUSES:
            return False
        if self.options.mode == "all":
            return True
        return row_config != config_hash or row_catalog != ref_hash  # incremental: outdated engine results

    def _match(
        self,
        selected: list[ProductSource],
        reference_sources: list[ProductSource],
        competitor_text: TextOptions,
        reference_text: TextOptions,
        current: dict[int, tuple[str, str | None, str | None, int]],
        config_hash: str,
        ref_hash: str,
    ) -> None:
        reference_docs = [build_doc(s, reference_text, self.config) for s in reference_sources]
        docs = [build_doc(s, competitor_text, self.config) for s in selected]
        texts = {d.product_id: d.embedding_text for d in reference_docs + docs}
        embedder = self.runtime.embedder_factory()
        try:
            vectors = EmbeddingCache(self.factory, embedder).ensure(
                texts,
                progress=lambda done, total: self._report("embed", done, total),
                check_stop=self.observer.check_stop,
                stats=self.embedding_stats,
            )
        finally:
            close = getattr(embedder, "close", None)
            if close:
                close()
        index = ReferenceIndex(
            reference_docs,
            np.vstack([vectors[d.product_id] for d in reference_docs])
            if reference_docs
            else np.zeros((0, self.config.embedding.dimensions)),
            self.config,
        )
        engine = MatchEngine(self.config, index)
        with transaction(self.factory) as session:
            rejected = {
                pid: frozenset(ids or [])
                for pid, ids in session.execute(
                    select(ProductMatch.product_id, ProductMatch.rejected_antalis_product_ids).where(
                        ProductMatch.competitor_id == self.competitor_id
                    )
                )
            }
        hashes = {d.product_id: d.product_hash for d in reference_docs}
        total = len(docs)
        self._report("match", 0, total)
        for start in range(0, total, WRITE_BATCH):
            self.observer.check_stop()
            batch = docs[start : start + WRITE_BATCH]
            outcomes = [
                (
                    doc,
                    engine.match(doc, vectors[doc.product_id], rejected_ids=rejected.get(doc.product_id, frozenset())),
                )
                for doc in batch
            ]
            with transaction(self.factory) as session:
                for doc, outcome in outcomes:
                    expected = current.get(doc.product_id)
                    self._persist(
                        session, doc, outcome, expected[3] if expected else None, hashes, config_hash, ref_hash
                    )
            self._report("match", min(start + WRITE_BATCH, total), total)

    def _persist(
        self,
        session: Session,
        doc: ProductDoc,
        outcome: MatchOutcome,
        expected_revision: int | None,
        reference_hashes: dict[int, str],
        config_hash: str,
        ref_hash: str,
    ) -> None:
        match = session.scalar(select(ProductMatch).where(ProductMatch.product_id == doc.product_id).with_for_update())
        if match is not None and match.revision != (expected_revision if expected_revision is not None else -1):
            # changed since this run read it (a reviewer decided meanwhile): keep their version
            self.counters["skipped_concurrent_change"] += 1
            return
        if match is None and expected_revision is not None:
            self.counters["skipped_concurrent_change"] += 1
            return
        best = outcome.best
        new_target = best.product_id if best and outcome.status == "auto_matched" else None
        proposed = best.product_id if best else None
        reasons = list(outcome.reasons)
        if match is None:
            match = ProductMatch(competitor_id=doc.competitor_id, product_id=doc.product_id, revision=0)
            session.add(match)
            previous_status, previous_target, snapshot, event = None, None, None, "computed"
        else:
            previous_status = match.status
            previous_target = match.antalis_product_id or match.proposed_antalis_product_id
            snapshot = _snapshot(match, session)
            event = "recomputed"
            if (
                match.status == "deprecated"
                and match.previous_status == "validated"
                and snapshot.get("antalis_product_id") == proposed
            ):
                reasons.append(
                    {
                        "code": "previously_validated",
                        "message": "this candidate was validated by a reviewer before the change",
                    }
                )
            elif (
                match.status == "deprecated"
                and snapshot.get("antalis_product_id")
                and snapshot["antalis_product_id"] != proposed
            ):
                reasons.append(
                    {
                        "code": "previous_association_changed",
                        "message": f"previously associated with product #{snapshot['antalis_product_id']}",
                    }
                )
        match.status = outcome.status
        match.antalis_product_id = new_target
        match.proposed_antalis_product_id = proposed
        match.score = Decimal(str(best.score)) if best else None
        match.runner_up_score = Decimal(str(outcome.runner_up_score)) if outcome.runner_up_score is not None else None
        match.score_gap = Decimal(str(outcome.gap)) if outcome.gap is not None else None
        match.dimensions_compared = bool(best and best.dimensions is not None)
        match.decision_reasons = reasons
        match.product_hash = doc.product_hash
        match.antalis_product_hash = (
            reference_hashes.get(proposed) if proposed and outcome.status != "no_match" else None
        )
        match.reference_catalog_hash = ref_hash
        match.config_hash = config_hash
        match.computed_at = utcnow()
        match.job_id = self.job_id
        match.decided_by = match.decided_at = match.decision_comment = None
        match.manual_choice = False
        match.deprecated_at = match.deprecated_reason = match.previous_status = None
        match.revision = (match.revision or 0) + 1
        session.flush()
        session.execute(delete(ProductMatchCandidate).where(ProductMatchCandidate.match_id == match.id))
        for candidate in outcome.candidates:
            session.add(
                ProductMatchCandidate(
                    match_id=match.id,
                    antalis_product_id=candidate.product_id,
                    rank=candidate.rank,
                    score=Decimal(str(candidate.score)),
                    semantic_score=_dec4(candidate.semantic),
                    lexical_score=_dec4(candidate.lexical),
                    dimension_score=_dec4(candidate.dimensions),
                    excluded=candidate.excluded,
                    details=candidate.details,
                )
            )
        changed = previous_status != match.status or previous_target != (new_target or proposed)
        if event == "computed" or changed or previous_status == "deprecated":
            _event(
                session,
                match,
                event,
                from_status=previous_status,
                from_antalis=previous_target,
                actor=ENGINE_ACTOR,
                reason="; ".join(r["message"] for r in reasons),
                job_id=self.job_id,
                data={"previous": snapshot} if snapshot else {},
            )
        self.counters["processed"] += 1
        self.counters[outcome.status] += 1
        if event == "recomputed" and changed:
            self.counters["changed"] += 1

    def _notify_summary(self, session: Session, name: str) -> None:
        totals = status_counts(session, self.competitor_id)
        review = totals.get("needs_review", 0)
        deprecated = totals.get("deprecated", 0)
        processed = self.counters["processed"]
        stats = self.embedding_stats
        title = (
            f"Matching finished: {name}, {review} product(s) need review"
            if review
            else f"Matching finished: {name}, nothing to review"
        )
        message = (
            f"This run ({self.options.mode}): {processed} product(s) matched - "
            f"{self.counters['auto_matched']} automatic, {self.counters['needs_review']} to review, "
            f"{self.counters['no_match']} without match"
            + (f", {self.counters['changed']} result(s) changed" if self.counters["changed"] else "")
            + f". Whole catalog: {', '.join(f'{n} {s}' for s, n in sorted(totals.items())) or 'no match yet'}"
            + (f" ({deprecated} deprecated awaiting a rerun)" if deprecated else "")
            + f". Embeddings: {stats.computed} computed ({stats.tokens} tokens), {stats.reused} reused."
        )
        notify(
            session,
            level="warning" if review or deprecated else "success",
            kind="matching_summary",
            title=title,
            message=message,
            competitor_id=self.competitor_id,
            job_id=self.job_id,
            data={
                "mode": self.options.mode,
                "needs_review": review,
                "totals": totals,
                "run": {**self.counters, **stats.as_dict()},
            },
        )


def _dec4(value: float | None) -> Decimal | None:
    return Decimal(str(round(value, 4))) if value is not None else None


def status_counts(session: Session, competitor_id: int) -> dict[str, int]:
    return dict(
        session.execute(
            select(ProductMatch.status, func.count())
            .where(ProductMatch.competitor_id == competitor_id)
            .group_by(ProductMatch.status)
        ).all()
    )


# --------------------------------------------------------------------------- human decisions


def _matchable_product(session: Session, product_id: int, config: MatchingConfig) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise MatchingError("product_not_found", f"product {product_id} not found")
    reference = reference_competitor(session, config)
    if reference is not None and product.competitor_id == reference.id:
        raise MatchingError("matching_reference", "reference products are not matched")
    return product


def _locked_match(session: Session, product: Product) -> ProductMatch | None:
    return session.scalar(select(ProductMatch).where(ProductMatch.product_id == product.id).with_for_update())


def _current_hash(session: Session, product_id: int) -> str | None:
    source = sources_by_id(session, [product_id]).get(product_id)
    return product_hash(source) if source else None


def validate_match(
    session: Session,
    product_id: int,
    config: MatchingConfig,
    *,
    antalis_product_id: int | None = None,
    decided_by: str | None = None,
    comment: str | None = None,
) -> ProductMatch:
    """Confirm the proposal, or associate another reference product chosen by the reviewer."""
    product = _matchable_product(session, product_id, config)
    match = _locked_match(session, product)
    target_id = antalis_product_id or (
        (match.antalis_product_id or match.proposed_antalis_product_id) if match is not None else None
    )
    if target_id is None:
        raise MatchingError("antalis_product_required", "no proposal to validate: give antalis_product_id")
    target = session.get(Product, target_id)
    reference = reference_competitor(session, config)
    if target is None or reference is None or target.competitor_id != reference.id:
        raise MatchingError("invalid_antalis_product", f"product {target_id} is not a product of the reference catalog")
    if match is None:
        match = ProductMatch(competitor_id=product.competitor_id, product_id=product.id, revision=0, status="validated")
        session.add(match)
        session.flush()
        previous_status, previous_target, snapshot = None, None, {}
    else:
        previous_status = match.status
        previous_target = match.antalis_product_id or match.proposed_antalis_product_id
        snapshot = _snapshot(match, session)
    match.status = "validated"
    match.antalis_product_id = target.id
    match.manual_choice = target.id != match.proposed_antalis_product_id
    match.decided_by = decided_by
    match.decided_at = utcnow()
    match.decision_comment = comment
    match.product_hash = _current_hash(session, product.id)
    match.antalis_product_hash = _current_hash(session, target.id)
    match.rejected_antalis_product_ids = [i for i in (match.rejected_antalis_product_ids or []) if i != target.id]
    match.deprecated_at = match.deprecated_reason = match.previous_status = None
    match.revision = (match.revision or 0) + 1
    _event(
        session,
        match,
        "validated",
        from_status=previous_status,
        from_antalis=previous_target,
        actor=decided_by or "user",
        reason=comment or ("manual choice of another product" if match.manual_choice else "proposal confirmed"),
        data={"previous": snapshot, "manual_choice": match.manual_choice},
    )
    session.flush()
    return match


def reject_match(
    session: Session,
    product_id: int,
    config: MatchingConfig,
    *,
    decided_by: str | None = None,
    comment: str | None = None,
) -> ProductMatch:
    """No equivalent in the reference catalog. The rejected product is remembered: the engine
    never proposes it again for this competitor product."""
    product = _matchable_product(session, product_id, config)
    match = _locked_match(session, product)
    if match is None:
        match = ProductMatch(competitor_id=product.competitor_id, product_id=product.id, revision=0, status="rejected")
        session.add(match)
        session.flush()
        previous_status, previous_target, snapshot = None, None, {}
    else:
        previous_status = match.status
        previous_target = match.antalis_product_id or match.proposed_antalis_product_id
        snapshot = _snapshot(match, session)
    rejected = list(match.rejected_antalis_product_ids or [])
    if previous_target is not None and previous_target not in rejected:
        rejected.append(previous_target)
    match.status = "rejected"
    match.antalis_product_id = None
    match.manual_choice = False
    match.rejected_antalis_product_ids = rejected
    match.decided_by = decided_by
    match.decided_at = utcnow()
    match.decision_comment = comment
    match.product_hash = _current_hash(session, product.id)
    match.antalis_product_hash = None
    match.deprecated_at = match.deprecated_reason = match.previous_status = None
    match.revision = (match.revision or 0) + 1
    _event(
        session,
        match,
        "rejected",
        from_status=previous_status,
        from_antalis=previous_target,
        actor=decided_by or "user",
        reason=comment or "no equivalent in the reference catalog",
        data={"previous": snapshot},
    )
    session.flush()
    return match


def reset_match(
    session: Session,
    product_id: int,
    config: MatchingConfig,
    *,
    decided_by: str | None = None,
    clear_rejections: bool = False,
) -> ProductMatch:
    """Send a match back to the engine: it becomes ``deprecated`` (reason ``reset_requested``)
    and is recomputed by the next incremental or deprecated run."""
    product = _matchable_product(session, product_id, config)
    match = _locked_match(session, product)
    if match is None:
        raise MatchingError("match_not_found", f"product {product_id} has no match yet")
    if clear_rejections:
        match.rejected_antalis_product_ids = []
    if match.status == "deprecated":
        match.deprecated_reason = "reset_requested"
        match.revision += 1
    else:
        _deprecate(session, match, "reset_requested", actor=decided_by or "user")
    session.flush()
    return match


# --------------------------------------------------------------------------- read side


def summary(session: Session, competitor: Competitor, runtime: MatchingRuntime | None) -> dict[str, Any]:
    counts = status_counts(session, competitor.id)
    eligible = session.scalar(select(func.count()).select_from(eligible_products(competitor.id).subquery()))
    matched = select(ProductMatch.product_id).where(ProductMatch.competitor_id == competitor.id)
    never = session.scalar(
        select(func.count()).select_from(eligible_products(competitor.id).where(Product.id.not_in(matched)).subquery())
    )
    tokens = session.scalar(
        select(func.coalesce(func.sum(ProductEmbedding.tokens), 0))
        .join(Product, Product.id == ProductEmbedding.product_id)
        .where(Product.competitor_id == competitor.id)
    )
    outdated = None
    if runtime is not None:
        options = (competitor.config or {}).get("matching") or {}
        outdated = session.scalar(
            select(func.count())
            .select_from(ProductMatch)
            .where(
                ProductMatch.competitor_id == competitor.id,
                ProductMatch.status.in_(MATCH_ENGINE_STATUSES),
                ProductMatch.config_hash.is_distinct_from(runtime.config.config_hash(options)),
            )
        )
    last = session.scalar(select(func.max(ProductMatch.computed_at)).where(ProductMatch.competitor_id == competitor.id))
    return {
        "counts": {
            status: counts.get(status, 0) for status in (*MATCH_ENGINE_STATUSES, *MATCH_HUMAN_STATUSES, "deprecated")
        },
        "eligible_products": eligible,
        "never_matched": never,
        "needs_review": counts.get("needs_review", 0),
        "deprecated": counts.get("deprecated", 0),
        "outdated_by_config": outdated,
        "embedding_tokens": tokens,
        "last_computed_at": last,
    }


def compare_with(
    session: Session,
    product_id: int,
    antalis_product_id: int,
    runtime: MatchingRuntime,
    competitor_options: dict[str, Any] | None,
) -> tuple[ProductDoc, Any]:
    """Score breakdown of one competitor product against one chosen reference product, from
    cached embeddings only (no API call: the semantic part is left out if one is missing)."""
    config = runtime.config
    product = _matchable_product(session, product_id, config)
    reference = reference_competitor(session, config)
    target = session.get(Product, antalis_product_id)
    if target is None or reference is None or target.competitor_id != reference.id:
        raise MatchingError("invalid_antalis_product", f"product {antalis_product_id} is not a reference product")
    reference_sources = load_sources(session, Product.id.in_(eligible_products(reference.id)))
    if all(s.product_id != target.id for s in reference_sources):
        reference_sources += sources_by_id(session, [target.id]).values()
    reference_docs = [build_doc(s, text_options(config, None), config) for s in reference_sources]
    doc = build_doc(sources_by_id(session, [product.id])[product.id], text_options(config, competitor_options), config)
    cached = load_cached(session, [d.product_id for d in reference_docs] + [doc.product_id], config.embedding)
    dims = config.embedding.dimensions
    has_vectors = doc.product_id in cached and target.id in cached
    vectors = np.vstack(
        [
            cached[d.product_id][1] if d.product_id in cached else np.zeros(dims, dtype=np.float32)
            for d in reference_docs
        ]
    )
    engine = MatchEngine(config, ReferenceIndex(reference_docs, vectors, config))
    candidate = engine.compare(doc, cached[doc.product_id][1] if has_vectors else None, target.id)
    return doc, candidate
