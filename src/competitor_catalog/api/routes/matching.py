"""Matching of competitor products with the reference catalog (Antalis).

A match is identified by its competitor product (``product_id``): there is exactly one per
competitor product. Runs are worker jobs (task ``matching``), followed like any other job.
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session, aliased

from competitor_catalog.api import schemas, views
from competitor_catalog.api.deps import (
    ApiError,
    ConfigsDep,
    MatchingDep,
    PageDep,
    SessionDep,
    competitor_id_filter,
    competitor_or_404,
    not_found,
    require_api_key,
)
from competitor_catalog.api.routes.competitors import refusal as job_refusal
from competitor_catalog.db.models import (
    JOB_ACTIVE_STATUSES,
    Category,
    Competitor,
    Product,
    ProductMatch,
    ProductMatchCandidate,
    ProductMatchEvent,
    WorkerJob,
)
from competitor_catalog.matching import service
from competitor_catalog.matching.engine import Candidate, build_doc
from competitor_catalog.matching.service import MatchingError, MatchingRuntime
from competitor_catalog.services import jobs as job_service

router = APIRouter(prefix="/matching", tags=["matching"], dependencies=[Depends(require_api_key)])

MATCHING_ERRORS = {
    "product_not_found": 404,
    "match_not_found": 404,
    "matching_reference": 422,
    "antalis_product_required": 422,
    "invalid_antalis_product": 422,
}
SortKey = Literal["score", "score_gap", "updated_at", "computed_at", "decided_at", "deprecated_at", "title", "id"]
CompetitorProduct = aliased(Product, name="competitor_product")
AssociatedProduct = aliased(Product, name="associated_product")
ProposedProduct = aliased(Product, name="proposed_product")
SORTS = {
    "score": ProductMatch.score,
    "score_gap": ProductMatch.score_gap,
    "updated_at": ProductMatch.updated_at,
    "computed_at": ProductMatch.computed_at,
    "decided_at": ProductMatch.decided_at,
    "deprecated_at": ProductMatch.deprecated_at,
    "title": CompetitorProduct.title,
    "id": ProductMatch.id,
}


def matching_error(exc: MatchingError) -> ApiError:
    return ApiError(MATCHING_ERRORS.get(exc.code, 409), exc.code, exc.message)


# --------------------------------------------------------------------------- views


def _briefs(session: Session, ids: set[int]) -> dict[int, schemas.ProductBriefOut]:
    ids.discard(None)
    if not ids:
        return {}
    keys = views.competitor_keys(session)
    rows = session.execute(
        select(Product, Category.name)
        .outerjoin(Category, Category.id == Product.category_id)
        .where(Product.id.in_(ids))
    ).all()
    return {
        p.id: schemas.ProductBriefOut(
            id=p.id,
            competitor_id=p.competitor_id,
            competitor_key=keys.get(p.competitor_id, "?"),
            catalog_reference=p.catalog_reference,
            title=p.title,
            url=p.url,
            brand=p.brand,
            category=category,
            status=p.status,
            normalized_attributes=p.normalized_attributes,
            image_url=views.image_url(p.images),
        )
        for p, category in rows
    }


def _config_hashes(session: Session, matching: MatchingRuntime, configs: dict) -> dict[int, str]:
    return {
        cid: matching.config.config_hash(configs[key].matching if key in configs else None)
        for cid, key in views.competitor_keys(session).items()
    }


def _items(
    session: Session, matches: list[ProductMatch], matching: MatchingRuntime, configs: dict
) -> list[schemas.MatchListItem]:
    ids = set()
    for m in matches:
        ids.update((m.product_id, m.antalis_product_id, m.proposed_antalis_product_id))
    briefs = _briefs(session, ids)
    keys = views.competitor_keys(session)
    hashes = _config_hashes(session, matching, configs)
    return [
        schemas.MatchListItem(**_item_fields(m, briefs, keys, hashes.get(m.competitor_id)))
        for m in matches
        if m.product_id in briefs
    ]


def _item_fields(m: ProductMatch, briefs: dict, keys: dict, config_hash: str | None) -> dict[str, Any]:
    return {
        "id": m.id,
        "competitor_id": m.competitor_id,
        "competitor_key": keys.get(m.competitor_id, "?"),
        "status": m.status,
        "product": briefs[m.product_id],
        "antalis_product": briefs.get(m.antalis_product_id),
        "proposed_antalis_product": briefs.get(m.proposed_antalis_product_id),
        "score": _f(m.score),
        "runner_up_score": _f(m.runner_up_score),
        "score_gap": _f(m.score_gap),
        "dimensions_compared": m.dimensions_compared,
        "decision_reasons": m.decision_reasons or [],
        "decided_by": m.decided_by,
        "decided_at": m.decided_at,
        "decision_comment": m.decision_comment,
        "manual_choice": m.manual_choice,
        "deprecated_at": m.deprecated_at,
        "deprecated_reason": m.deprecated_reason,
        "previous_status": m.previous_status,
        "computed_at": m.computed_at,
        "job_id": m.job_id,
        "outdated_config": m.status in ("auto_matched", "needs_review", "no_match")
        and m.config_hash is not None
        and m.config_hash != config_hash,
        "revision": m.revision,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    }


def _f(value) -> float | None:
    return float(value) if value is not None else None


def _candidate_out(
    candidate: dict[str, Any], brief: schemas.ProductBriefOut, match: ProductMatch | None
) -> schemas.CandidateOut:
    details = candidate["details"]
    pid = brief.id
    return schemas.CandidateOut(
        rank=candidate["rank"],
        excluded=candidate["excluded"],
        antalis_product=brief,
        score=candidate["score"],
        semantic_score=candidate["semantic"],
        lexical_score=candidate["lexical"],
        dimension_score=candidate["dimensions"],
        is_proposed=match is not None and match.proposed_antalis_product_id == pid,
        is_associated=match is not None and match.antalis_product_id == pid,
        rejected_by_human=match is not None and pid in (match.rejected_antalis_product_ids or []),
        weights=details.get("weights", {}),
        semantic=details.get("semantic", {"cosine": None, "score": None}),
        lexical=details.get("lexical", {"cosine": None, "score": None}),
        dimensions=details.get("dimensions", {"comparable": False, "score": None, "measures": []}),
        features=details.get("features", []),
        differences=details.get("differences", []),
        exclusions=details.get("exclusions", []),
        review_flags=details.get("review_flags", []),
        retrieved_by=details.get("retrieved_by", []),
    )


def _engine_candidate(c: Candidate) -> dict[str, Any]:
    return {
        "rank": c.rank,
        "excluded": c.excluded,
        "score": c.score,
        "semantic": c.semantic,
        "lexical": c.lexical,
        "dimensions": c.dimensions,
        "details": c.details,
    }


def _events_out(session: Session, events: list[ProductMatchEvent]) -> list[schemas.MatchEventOut]:
    titles = dict(
        session.execute(select(Product.id, Product.title).where(Product.id.in_({e.product_id for e in events}))).all()
    )
    return [
        schemas.MatchEventOut(
            id=e.id,
            match_id=e.match_id,
            product_id=e.product_id,
            competitor_id=e.competitor_id,
            event=e.event,
            from_status=e.from_status,
            to_status=e.to_status,
            from_antalis_product_id=e.from_antalis_product_id,
            to_antalis_product_id=e.to_antalis_product_id,
            score=_f(e.score),
            actor=e.actor,
            reason=e.reason,
            job_id=e.job_id,
            data=e.data or {},
            created_at=e.created_at,
            product_title=titles.get(e.product_id),
        )
        for e in events
    ]


def _detail(session: Session, match: ProductMatch, matching: MatchingRuntime, configs: dict) -> schemas.MatchDetailOut:
    candidates = list(
        session.scalars(
            select(ProductMatchCandidate)
            .where(ProductMatchCandidate.match_id == match.id)
            .order_by(ProductMatchCandidate.excluded, ProductMatchCandidate.rank, ProductMatchCandidate.score.desc())
        )
    )
    rejected = [int(i) for i in match.rejected_antalis_product_ids or []]
    ids = {match.product_id, match.antalis_product_id, match.proposed_antalis_product_id, *rejected}
    ids.update(c.antalis_product_id for c in candidates)
    briefs = _briefs(session, ids)
    keys = views.competitor_keys(session)
    hashes = _config_hashes(session, matching, configs)
    events = list(
        session.scalars(
            select(ProductMatchEvent)
            .where(ProductMatchEvent.match_id == match.id)
            .order_by(ProductMatchEvent.created_at.desc(), ProductMatchEvent.id.desc())
        )
    )
    return schemas.MatchDetailOut(
        **_item_fields(match, briefs, keys, hashes.get(match.competitor_id)),
        analysis=_analysis(session, match.product_id, match.competitor_id, matching, configs),
        candidates=[
            _candidate_out(
                {
                    "rank": c.rank,
                    "excluded": c.excluded,
                    "score": float(c.score),
                    "semantic": _f(c.semantic_score),
                    "lexical": _f(c.lexical_score),
                    "dimensions": _f(c.dimension_score),
                    "details": c.details or {},
                },
                briefs[c.antalis_product_id],
                match,
            )
            for c in candidates
            if c.antalis_product_id in briefs
        ],
        rejected_antalis_products=[briefs[i] for i in rejected if i in briefs],
        history=_events_out(session, events),
    )


def _competitor_options(session: Session, competitor_id: int, configs: dict) -> dict[str, Any]:
    key = views.competitor_keys(session).get(competitor_id)
    return configs[key].matching if key in configs else {}


def _analysis(
    session: Session, product_id: int, competitor_id: int, matching: MatchingRuntime, configs: dict
) -> schemas.ProductAnalysisOut | None:
    source = service.sources_by_id(session, [product_id]).get(product_id)
    if source is None:
        return None
    options = service.text_options(matching.config, _competitor_options(session, competitor_id, configs))
    return schemas.ProductAnalysisOut(**build_doc(source, options, matching.config).summary())


def _match_or_404(session: Session, product_id: int) -> ProductMatch:
    match = session.scalar(select(ProductMatch).where(ProductMatch.product_id == product_id))
    if match is None:
        if session.get(Product, product_id) is None:
            raise not_found("product", product_id)
        raise ApiError(404, "match_not_found", f"product {product_id} has not been matched yet")
    return match


# --------------------------------------------------------------------------- config / summary


@router.get("/config", response_model=schemas.MatchingConfigOut, summary="Matching configuration (thresholds...)")
def get_config(matching: MatchingDep):
    """Thresholds and weights are edited in ``config/matching.yaml`` (redeploy); a change makes
    the next incremental run recompute the engine results, human decisions are kept."""
    cfg = matching.config
    return schemas.MatchingConfigOut(
        reference_competitor=cfg.reference_competitor,
        configured=matching.configured,
        engine_version=cfg.public()["engine_version"],
        thresholds={"auto_match": cfg.auto_match_threshold, "min_gap": cfg.min_gap, "review": cfg.review_threshold},
        weights={"with_dimensions": cfg.weights_with_dimensions, "without_dimensions": cfg.weights_without_dimensions},
        embedding={
            "model": cfg.embedding.model,
            "dimensions": cfg.embedding.dimensions,
            "input_type": cfg.embedding.input_type,
        },
        config=cfg.raw,
    )


def summary_out(session: Session, competitor: Competitor, matching: MatchingRuntime) -> schemas.MatchingSummaryOut:
    code = service.refusal(session, competitor, matching)
    active = job_service.active_job(session, competitor.id)
    if code is None and active is not None:
        code = "worker_busy"
    jobs = select(WorkerJob).where(WorkerJob.competitor_id == competitor.id, WorkerJob.task == "matching")
    current = session.scalar(jobs.where(WorkerJob.status.in_(JOB_ACTIVE_STATUSES)).limit(1))
    last = session.scalar(
        jobs.where(WorkerJob.status.not_in(JOB_ACTIVE_STATUSES)).order_by(WorkerJob.created_at.desc()).limit(1)
    )
    data = service.summary(session, competitor, matching)
    return schemas.MatchingSummaryOut(
        competitor_id=competitor.id,
        competitor_key=competitor.key,
        competitor_name=competitor.name,
        availability=schemas.MatchingAvailability(
            allowed=code is None,
            reason=code,
            message=service.REFUSALS.get(code) if code != "worker_busy" else f"job #{active.id} is {active.status}",
        ),
        job=views.job_out(current, competitor.key) if current else None,
        last_job=views.job_out(last, competitor.key) if last else None,
        **data,
    )


@router.get("/summary", response_model=list[schemas.MatchingSummaryOut], summary="Matching state per competitor")
def get_summary(
    session: SessionDep,
    matching: MatchingDep,
    competitor: Annotated[str | None, Query(description="competitor key; default: every competitor")] = None,
):
    """Counts per status, products never matched, items to review, deprecated matches,
    whether a run can start (prerequisites: full catalog sync of the competitor and of the
    reference catalog, VOYAGE_API_KEY), the active/last matching job. The reference
    competitor is not listed."""
    if competitor:
        competitors = [competitor_or_404(session, competitor)]
    else:
        competitors = list(session.scalars(select(Competitor).order_by(Competitor.name)))
    return [summary_out(session, c, matching) for c in competitors if c.key != matching.config.reference_competitor]


# --------------------------------------------------------------------------- runs


def _enqueue(session: Session, key: str, options: dict, configs: dict, matching: MatchingRuntime):
    competitor = competitor_or_404(session, key)
    config = configs.get(competitor.key)
    if config is None:
        raise ApiError(409, "competitor_not_configured", f"no configuration file for {competitor.key}")
    job = job_service.enqueue_job(
        session, competitor, config, "matching", options, requested_by="api", matching=matching
    )
    return competitor, job


@router.post(
    "/competitors/{key}/run",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Match the products of a competitor",
    responses={409: {"model": schemas.ErrorResponse}, 422: {"model": schemas.ErrorResponse}},
)
def run_matching(
    key: str,
    session: SessionDep,
    configs: ConfigsDep,
    matching: MatchingDep,
    body: Annotated[
        schemas.MatchingOptionsIn,
        Body(
            examples=[
                {"mode": "incremental"},
                {"mode": "deprecated"},
                {"mode": "all"},
                {"mode": "all", "reembed": True},
            ]
        ),
    ] = None,
):
    """Queues a matching job (followed like any job: ``GET /jobs/{id}``, worker endpoints,
    notifications). Default mode ``incremental``: what changed since the last run only.
    Whatever the mode, texts already embedded are never sent to Voyage AI again unless
    ``reembed`` is true: rerunning ``mode=all`` after a scoring change costs no embedding.

    Refusals (HTTP 409): ``matching_reference``, ``catalog_required`` (no full catalog sync of
    the competitor yet: its products are not parsed), ``reference_catalog_required``,
    ``reference_missing``, ``matching_not_configured``, ``worker_busy``, ``competitor_disabled``.
    At the end, a ``matching_summary`` notification gives the number of products to review."""
    body = body or schemas.MatchingOptionsIn()
    try:
        competitor, job = _enqueue(session, key, body.model_dump(), configs, matching)
    except job_service.JobRequestError as exc:
        raise job_refusal(exc) from None
    session.commit()
    return views.job_out(job, competitor.key)


@router.post(
    "/deprecated/rerun",
    response_model=schemas.RerunOut,
    status_code=202,
    summary="Recompute every deprecated match",
)
def rerun_deprecated(
    session: SessionDep,
    configs: ConfigsDep,
    matching: MatchingDep,
    body: Annotated[schemas.RerunIn, Body(examples=[{}, {"competitor": "packdiscount_fr"}])] = None,
):
    """Queues one ``mode=deprecated`` matching job per competitor that has deprecated matches
    (nothing else is recomputed). Competitors whose worker is busy are reported in ``refused``."""
    body = body or schemas.RerunIn()
    stmt = select(Competitor.key).join(ProductMatch, ProductMatch.competitor_id == Competitor.id)
    stmt = stmt.where(ProductMatch.status == "deprecated").group_by(Competitor.key)
    if body.competitor:
        stmt = stmt.where(Competitor.id == competitor_or_404(session, body.competitor).id)
    with_deprecated = set(session.scalars(stmt))
    candidates = (
        [body.competitor]
        if body.competitor
        else [
            k
            for k in session.scalars(select(Competitor.key).order_by(Competitor.key))
            if k != matching.config.reference_competitor
        ]
    )
    result = schemas.RerunOut(jobs=[], refused=[], nothing_to_do=[])
    for key in candidates:
        competitor_key = competitor_or_404(session, key).key
        if competitor_key not in with_deprecated:
            result.nothing_to_do.append(competitor_key)
            continue
        try:
            with session.begin_nested():
                competitor, job = _enqueue(session, competitor_key, {"mode": "deprecated"}, configs, matching)
            result.jobs.append(views.job_out(job, competitor.key))
        except (job_service.JobRequestError, ApiError) as exc:
            result.refused.append(
                schemas.RerunRefusal(competitor_key=competitor_key, code=exc.code, message=exc.message)
            )
    session.commit()
    return result


# --------------------------------------------------------------------------- lists


def _filtered(
    session: Session,
    *,
    competitor: str | None,
    status: list[str] | None,
    category_id: list[int] | None,
    q: str | None,
    antalis_q: str | None,
    antalis_product_id: int | None,
    min_score: float | None,
    max_score: float | None,
    decided: bool | None,
    manual_choice: bool | None,
    reason: list[str] | None,
    deprecated_reason: list[str] | None,
    dimensions_compared: bool | None,
    product_status: str | None,
) -> Select:
    stmt = select(ProductMatch).join(CompetitorProduct, CompetitorProduct.id == ProductMatch.product_id)
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.where(ProductMatch.competitor_id == competitor_id)
    if status:
        stmt = stmt.where(ProductMatch.status.in_(status))
    if category_id:
        # A product is in a category when its own category is that one or one of its descendants.
        tree = select(Category.id).where(Category.id.in_(category_id)).cte("category_tree", recursive=True)
        tree = tree.union(select(Category.id).join(tree, Category.parent_id == tree.c.id))
        stmt = stmt.where(CompetitorProduct.category_id.in_(select(tree.c.id)))
    for word in (q or "").split():
        like = f"%{word}%"
        stmt = stmt.where(
            or_(
                CompetitorProduct.title.ilike(like),
                CompetitorProduct.catalog_reference.ilike(like),
                CompetitorProduct.brand.ilike(like),
            )
        )
    if antalis_q:
        stmt = stmt.outerjoin(AssociatedProduct, AssociatedProduct.id == ProductMatch.antalis_product_id)
        stmt = stmt.outerjoin(ProposedProduct, ProposedProduct.id == ProductMatch.proposed_antalis_product_id)
        for word in antalis_q.split():
            like = f"%{word}%"
            stmt = stmt.where(
                or_(
                    AssociatedProduct.title.ilike(like),
                    AssociatedProduct.catalog_reference.ilike(like),
                    ProposedProduct.title.ilike(like),
                    ProposedProduct.catalog_reference.ilike(like),
                )
            )
    if antalis_product_id is not None:
        stmt = stmt.where(
            or_(
                ProductMatch.antalis_product_id == antalis_product_id,
                ProductMatch.proposed_antalis_product_id == antalis_product_id,
            )
        )
    if min_score is not None:
        stmt = stmt.where(ProductMatch.score >= min_score)
    if max_score is not None:
        stmt = stmt.where(ProductMatch.score <= max_score)
    if decided is True:
        stmt = stmt.where(ProductMatch.decided_at.is_not(None))
    elif decided is False:
        stmt = stmt.where(ProductMatch.decided_at.is_(None))
    if manual_choice is not None:
        stmt = stmt.where(ProductMatch.manual_choice.is_(manual_choice))
    if reason:
        stmt = stmt.where(or_(*(ProductMatch.decision_reasons.contains([{"code": code}]) for code in reason)))
    if deprecated_reason:
        stmt = stmt.where(ProductMatch.deprecated_reason.in_(deprecated_reason))
    if dimensions_compared is not None:
        stmt = stmt.where(ProductMatch.dimensions_compared.is_(dimensions_compared))
    if product_status:
        stmt = stmt.where(CompetitorProduct.status == product_status)
    return stmt


def _list(session, page, matching, configs, stmt, sort: str, order: str) -> dict[str, Any]:
    column = SORTS[sort]
    stmt = stmt.order_by(column.desc().nulls_last() if order == "desc" else column.asc().nulls_last(), ProductMatch.id)
    result = page.page(session, stmt)
    result["items"] = _items(session, result["items"], matching, configs)
    return result


StatusQuery = Annotated[list[schemas.MatchStatus] | None, Query(description="repeatable")]
ReasonQuery = Annotated[
    list[str] | None, Query(description="decision reason code, repeatable (e.g. ambiguous_gap, feature_conflict)")
]
DeprecationQuery = Annotated[list[schemas.DeprecationReason] | None, Query(description="repeatable")]


class ListFilters:
    def __init__(
        self,
        competitor: Annotated[str | None, Query(description="competitor key")] = None,
        category_id: Annotated[
            list[int] | None,
            Query(description="competitor product in one of these categories or their sub-categories, repeatable"),
        ] = None,
        q: Annotated[str | None, Query(description="words in the competitor product title/reference/brand")] = None,
        antalis_q: Annotated[
            str | None, Query(description="words in the associated or proposed Antalis product title/reference")
        ] = None,
        antalis_product_id: Annotated[
            int | None, Query(description="matches associated with / proposing this Antalis product")
        ] = None,
        min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
        max_score: Annotated[float | None, Query(ge=0, le=100)] = None,
        decided: Annotated[bool | None, Query(description="true = decided by a human")] = None,
        manual_choice: Annotated[bool | None, Query(description="validated with another product")] = None,
        reason: ReasonQuery = None,
        dimensions_compared: bool | None = None,
        product_status: Annotated[schemas.LifecycleStatus | None, Query(description="competitor product")] = None,
    ):
        self.values = {
            "competitor": competitor,
            "category_id": category_id,
            "q": q,
            "antalis_q": antalis_q,
            "antalis_product_id": antalis_product_id,
            "min_score": min_score,
            "max_score": max_score,
            "decided": decided,
            "manual_choice": manual_choice,
            "reason": reason,
            "dimensions_compared": dimensions_compared,
            "product_status": product_status,
        }


FiltersDep = Annotated[ListFilters, Depends()]


@router.get("/matches", response_model=schemas.Page[schemas.MatchListItem], summary="List matches")
def list_matches(
    session: SessionDep,
    page: PageDep,
    matching: MatchingDep,
    configs: ConfigsDep,
    filters: FiltersDep,
    status: StatusQuery = None,
    deprecated_reason: DeprecationQuery = None,
    sort: SortKey = "updated_at",
    order: Literal["asc", "desc"] = "desc",
):
    """Every match with its status: ``auto_matched`` (automatic association), ``validated``
    (by a human, ``manual_choice`` when another product was chosen), ``needs_review``,
    ``no_match``, ``rejected`` (a human confirmed there is no equivalent), ``deprecated``."""
    stmt = _filtered(session, status=status, deprecated_reason=deprecated_reason, **filters.values)
    return _list(session, page, matching, configs, stmt, sort, order)


@router.get("/review", response_model=schemas.Page[schemas.MatchListItem], summary="Products that need a human review")
def review_queue(
    session: SessionDep,
    page: PageDep,
    matching: MatchingDep,
    configs: ConfigsDep,
    filters: FiltersDep,
    sort: SortKey = "score",
    order: Literal["asc", "desc"] = "desc",
):
    """Matches in ``needs_review``: best candidate between the review and auto thresholds, or
    above the auto threshold but ambiguous (second candidate too close) or with a conflicting
    characteristic. Decide with ``POST /matching/products/{product_id}/validate|reject``."""
    stmt = _filtered(session, status=["needs_review"], deprecated_reason=None, **filters.values)
    return _list(session, page, matching, configs, stmt, sort, order)


@router.get("/deprecated", response_model=schemas.Page[schemas.MatchListItem], summary="Obsolete matches")
def deprecated_matches(
    session: SessionDep,
    page: PageDep,
    matching: MatchingDep,
    configs: ConfigsDep,
    filters: FiltersDep,
    deprecated_reason: DeprecationQuery = None,
    sort: SortKey = "deprecated_at",
    order: Literal["asc", "desc"] = "desc",
):
    """Matches whose competitor or Antalis product changed (or disappeared) since they were
    computed or decided: ``previous_status`` and the association are kept as they were. They
    are recomputed by ``POST /matching/deprecated/rerun`` (or any incremental run); their
    former state stays in the history (``GET /matching/history?event=deprecated``)."""
    stmt = _filtered(session, status=["deprecated"], deprecated_reason=deprecated_reason, **filters.values)
    return _list(session, page, matching, configs, stmt, sort, order)


@router.get("/history", response_model=schemas.Page[schemas.MatchEventOut], summary="History of all matches")
def history(
    session: SessionDep,
    page: PageDep,
    competitor: Annotated[str | None, Query(description="competitor key")] = None,
    product_id: int | None = None,
    event: Annotated[list[schemas.MatchEventType] | None, Query(description="repeatable")] = None,
    job_id: int | None = None,
    actor: str | None = None,
):
    """Every computation that changed a result, every human decision and every deprecation,
    newest first, with a snapshot of the previous state in ``data``."""
    stmt = select(ProductMatchEvent).order_by(ProductMatchEvent.created_at.desc(), ProductMatchEvent.id.desc())
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.where(ProductMatchEvent.competitor_id == competitor_id)
    if product_id is not None:
        stmt = stmt.where(ProductMatchEvent.product_id == product_id)
    if event:
        stmt = stmt.where(ProductMatchEvent.event.in_(event))
    if job_id is not None:
        stmt = stmt.where(ProductMatchEvent.job_id == job_id)
    if actor:
        stmt = stmt.where(ProductMatchEvent.actor == actor)
    result = page.page(session, stmt)
    result["items"] = _events_out(session, result["items"])
    return result


# --------------------------------------------------------------------------- one product


@router.get(
    "/dimensionless",
    response_model=schemas.DimensionlessPage,
    summary="Products whose dimensions could not be read",
)
def dimensionless(
    session: SessionDep,
    page: PageDep,
    matching: MatchingDep,
    competitor: Annotated[str, Query(description="competitor key (the reference catalog works too)")],
    product_type: Annotated[
        str | None, Query(description="detected product type (box, tape...), 'unknown' = none detected")
    ] = None,
    title_has_numbers: Annotated[
        bool | None, Query(description="true = titles with numbers (likely parsing gaps)")
    ] = None,
):
    """Readable products (active, parsed, with a title) for which neither the normalized
    attributes nor the title give any measure, the way the matching engine reads them: their
    matching cannot compare dimensions. Use it to find attribute mappings and title formats
    to support. Sorted by product type, then title."""
    found = service.products_without_dimensions(session, competitor_or_404(session, competitor).id, matching.config)
    by_type = Counter(p.product_type or "unknown" for p in found)
    if product_type is not None:
        found = [p for p in found if (p.product_type or "unknown") == product_type]
    if title_has_numbers is not None:
        found = [p for p in found if p.title_has_numbers == title_has_numbers]
    found.sort(key=lambda p: (p.product_type or "~", p.source.title or ""))
    items = [
        schemas.DimensionlessProductOut(
            product_id=p.source.product_id,
            catalog_reference=p.source.reference,
            title=p.source.title,
            category=p.source.category,
            url=p.source.url,
            product_type=p.product_type,
            title_has_numbers=p.title_has_numbers,
            normalized_attributes=p.source.normalized_attributes or {},
        )
        for p in found[page.offset : page.offset + page.limit]
    ]
    return schemas.DimensionlessPage(
        items=items, total=len(found), limit=page.limit, offset=page.offset, by_product_type=dict(by_type)
    )


@router.get(
    "/products/{product_id}",
    response_model=schemas.MatchDetailOut,
    summary="A competitor product, its candidates and the score breakdown",
)
def get_product_match(product_id: int, session: SessionDep, matching: MatchingDep, configs: ConfigsDep):
    """For review: what the engine read of the product (``analysis``), each candidate with its
    sub-scores (semantic, lexical, dimensions), weights, compared measures and
    characteristics, differences, exclusions and review flags, the final score, the decision
    reasons and the full history."""
    return _detail(session, _match_or_404(session, product_id), matching, configs)


@router.get(
    "/products/{product_id}/compare/{antalis_product_id}",
    response_model=schemas.CompareOut,
    summary="Score breakdown against any Antalis product",
)
def compare(product_id: int, antalis_product_id: int, session: SessionDep, matching: MatchingDep, configs: ConfigsDep):
    """To check a manual choice before validating it. Uses the cached embeddings only (no
    Voyage call): ``semantic_available=false`` when one is missing."""
    product = session.get(Product, product_id)
    if product is None:
        raise not_found("product", product_id)
    try:
        doc, candidate = service.compare_with(
            session,
            product_id,
            antalis_product_id,
            matching,
            _competitor_options(session, product.competitor_id, configs),
        )
    except MatchingError as exc:
        raise matching_error(exc) from None
    match = session.scalar(select(ProductMatch).where(ProductMatch.product_id == product_id))
    briefs = _briefs(session, {product_id, antalis_product_id})
    return schemas.CompareOut(
        product=briefs[product_id],
        analysis=schemas.ProductAnalysisOut(**doc.summary()),
        candidate=_candidate_out(_engine_candidate(candidate), briefs[antalis_product_id], match),
        semantic_available=candidate.semantic is not None,
    )


def _decide(session, product_id: int, matching: MatchingRuntime, configs: dict, action) -> schemas.MatchDetailOut:
    try:
        match = action()
    except MatchingError as exc:
        session.rollback()
        raise matching_error(exc) from None
    session.commit()
    return _detail(session, match, matching, configs)


@router.post(
    "/products/{product_id}/validate",
    response_model=schemas.MatchDetailOut,
    summary="Validate the proposal or associate another Antalis product",
    responses={404: {"model": schemas.ErrorResponse}, 422: {"model": schemas.ErrorResponse}},
)
def validate(
    product_id: int,
    session: SessionDep,
    matching: MatchingDep,
    configs: ConfigsDep,
    body: Annotated[schemas.ValidateIn, Body(examples=[{}, {"antalis_product_id": 123, "decided_by": "alice"}])] = None,
):
    """Without ``antalis_product_id``: confirms the engine's proposal. With it: associates that
    Antalis product (``manual_choice=true`` when it is not the proposal). Status ``validated``;
    kept by every rerun until the competitor or the Antalis product changes."""
    body = body or schemas.ValidateIn()
    return _decide(
        session,
        product_id,
        matching,
        configs,
        lambda: service.validate_match(
            session,
            product_id,
            matching.config,
            antalis_product_id=body.antalis_product_id,
            decided_by=body.decided_by,
            comment=body.comment,
        ),
    )


@router.post(
    "/products/{product_id}/reject",
    response_model=schemas.MatchDetailOut,
    summary="Reject: no equivalent in the Antalis catalog",
    responses={404: {"model": schemas.ErrorResponse}, 422: {"model": schemas.ErrorResponse}},
)
def reject(
    product_id: int,
    session: SessionDep,
    matching: MatchingDep,
    configs: ConfigsDep,
    body: Annotated[schemas.RejectIn, Body(examples=[{}, {"decided_by": "alice", "comment": "other size"}])] = None,
):
    """Status ``rejected``; the rejected proposal is remembered and never proposed again for
    this product. To associate another product instead, use ``validate`` with its id."""
    body = body or schemas.RejectIn()
    return _decide(
        session,
        product_id,
        matching,
        configs,
        lambda: service.reject_match(
            session, product_id, matching.config, decided_by=body.decided_by, comment=body.comment
        ),
    )


@router.post(
    "/products/{product_id}/reset",
    response_model=schemas.MatchDetailOut,
    summary="Send a match back to the engine",
    responses={404: {"model": schemas.ErrorResponse}},
)
def reset(
    product_id: int,
    session: SessionDep,
    matching: MatchingDep,
    configs: ConfigsDep,
    body: Annotated[schemas.ResetIn, Body(examples=[{}, {"clear_rejections": True}])] = None,
):
    """The match becomes ``deprecated`` (reason ``reset_requested``, previous state kept in the
    history) and is recomputed by the next run (``mode=deprecated`` or ``incremental``)."""
    body = body or schemas.ResetIn()
    return _decide(
        session,
        product_id,
        matching,
        configs,
        lambda: service.reset_match(
            session,
            product_id,
            matching.config,
            decided_by=body.decided_by,
            clear_rejections=body.clear_rejections,
        ),
    )
