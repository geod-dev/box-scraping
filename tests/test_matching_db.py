"""Product matching against PostgreSQL: runs, reruns, change detection, human decisions, history."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from competitor_catalog.db.models import (
    Competitor,
    Notification,
    Product,
    ProductEmbedding,
    ProductMatch,
    ProductMatchCandidate,
    ProductMatchEvent,
)
from competitor_catalog.db.session import transaction
from competitor_catalog.matching import service
from competitor_catalog.matching.service import MatchingError, MatchingOptions, MatchingRun, MatchingRuntime
from competitor_catalog.services.runs import RunObserver
from tests.matching_fakes import COMPETITOR, Runtime, make_config, seed_catalogs

pytestmark = pytest.mark.db

PACKDISCOUNT_OPTIONS = {"ignore_description_patterns": ["^\\W*Voir toutes les r[ée]f[ée]rences"]}


@pytest.fixture
def env(factory):
    with transaction(factory) as session:
        ids = seed_catalogs(session)
        competitor_id = session.scalar(select(Competitor.id).where(Competitor.key == "packdiscount_fr"))
    return Env(factory, ids, competitor_id)


class Env:
    def __init__(self, factory, ids, competitor_id):
        self.factory = factory
        self.ids = ids
        self.competitor_id = competitor_id
        self.fake = Runtime()

    def run(self, mode="incremental", observer=None, runtime=None):
        return MatchingRun(
            self.factory,
            self.competitor_id,
            runtime or self.fake.runtime,
            MatchingOptions(mode),
            observer or RunObserver(),
            competitor_options=PACKDISCOUNT_OPTIONS,
        ).run()

    def match(self, ref: str) -> ProductMatch:
        with transaction(self.factory) as session:
            return session.scalar(select(ProductMatch).where(ProductMatch.product_id == self.ids[ref]))

    def statuses(self) -> dict[str, str]:
        refs = {v: k for k, v in self.ids.items()}
        with transaction(self.factory) as session:
            return {refs[m.product_id]: m.status for m in session.scalars(select(ProductMatch))}

    def events(self, ref: str) -> list[str]:
        with transaction(self.factory) as session:
            return list(
                session.scalars(
                    select(ProductMatchEvent.event)
                    .where(ProductMatchEvent.product_id == self.ids[ref])
                    .order_by(ProductMatchEvent.id)
                )
            )

    def notifications(self, kind: str) -> list[Notification]:
        with transaction(self.factory) as session:
            return list(
                session.scalars(select(Notification).where(Notification.kind == kind).order_by(Notification.id))
            )

    def update_product(self, ref: str, **values) -> None:
        with transaction(self.factory) as session:
            product = session.get(Product, self.ids[ref])
            for key, value in values.items():
                setattr(product, key, value)


def test_prerequisites(factory):
    fake = Runtime()
    with transaction(factory) as session:
        seed_catalogs(session, competitor_catalog=False)
        packdiscount = session.scalar(select(Competitor).where(Competitor.key == "packdiscount_fr"))
        antalis = session.scalar(select(Competitor).where(Competitor.key == "antalis_fr"))
        assert service.refusal(session, packdiscount, fake.runtime) == "catalog_required"
        assert service.refusal(session, antalis, fake.runtime) == "matching_reference"
        assert service.refusal(session, packdiscount, MatchingRuntime(fake.config)) == "matching_not_configured"
        packdiscount.last_full_catalog_at = antalis.last_full_catalog_at
        antalis.last_full_catalog_at = None
        assert service.refusal(session, packdiscount, fake.runtime) == "reference_catalog_required"
        packdiscount_id = packdiscount.id
    with pytest.raises(MatchingError) as error:
        MatchingRun(factory, packdiscount_id, fake.runtime, MatchingOptions()).run()
    assert error.value.code == "reference_catalog_required"


def test_first_run_matches_every_parsed_product(env):
    env.update_product("P3", details_updated_at=None)  # a product without parsed details is not eligible
    result = env.run()
    expected = {ref: status for ref, _t, _d, _a, status, _e in COMPETITOR if ref != "P3"}
    assert env.statuses() == expected
    assert result.counters["processed"] == 3 and result.counters["eligible_products"] == 3
    p1 = env.match("P1")
    assert p1.antalis_product_id == env.ids["A1"] == p1.proposed_antalis_product_id
    assert float(p1.score) >= 90 and p1.dimensions_compared and p1.product_hash and p1.antalis_product_hash
    p2 = env.match("P2")
    assert p2.antalis_product_id is None and p2.proposed_antalis_product_id in (env.ids["A4"], env.ids["A5"])
    with transaction(env.factory) as session:
        candidates = session.scalars(select(ProductMatchCandidate).where(ProductMatchCandidate.match_id == p1.id))
        by_product = {c.antalis_product_id: c for c in candidates}
        assert by_product[env.ids["A1"]].rank == 1 and by_product[env.ids["A3"]].excluded
        assert by_product[env.ids["A1"]].details["dimensions"]["comparable"] is True
        embedded = session.scalar(select(func.count()).select_from(ProductEmbedding))
        assert embedded == 7 + 3  # reference catalog + matched products
    assert env.events("P1") == ["computed"]
    summary = env.notifications("matching_summary")
    assert len(summary) == 1 and summary[0].data["needs_review"] == 1
    assert "1 product(s) need review" in summary[0].title


def test_rerun_without_changes_recomputes_nothing(env):
    env.run()
    sent = len(env.fake.embedder.texts)
    result = env.run()
    assert result.counters["selected"] == 0 and result.counters.get("processed", 0) == 0
    assert len(env.fake.embedder.texts) == sent  # every embedding reused
    assert env.events("P1") == ["computed"]
    result = env.run("all")  # engine results recomputed, embeddings still reused, same results: no event
    assert result.counters["processed"] == 4 and result.counters.get("changed", 0) == 0
    assert len(env.fake.embedder.texts) == sent and env.events("P1") == ["computed"]


def test_changed_product_deprecates_its_match_then_rerun_only_deprecated(env):
    env.run()
    env.update_product(
        "P1",
        title="Caisse carton simple cannelure 30,5 x 21,5 x 17 cm",
        normalized_attributes={"inner_length_mm": 305, "inner_width_mm": 215, "inner_height_mm": 170},
    )
    with transaction(env.factory) as session:
        deprecated = service.detect_stale_matches(session, [env.competitor_id])
    assert [(d.reason, d.previous_status) for d in deprecated] == [("product_changed", "auto_matched")]
    p1 = env.match("P1")
    assert p1.status == "deprecated" and p1.previous_status == "auto_matched"
    assert p1.antalis_product_id == env.ids["A1"]  # the former association stays visible
    note = env.notifications("matches_deprecated")[-1]
    assert note.level == "warning" and "Matching invalidated" in note.title and note.data["count"] == 1
    assert note.data["matches"][0]["reason"] == "product_changed"

    sent = len(env.fake.embedder.texts)
    result = env.run("deprecated")
    assert result.counters["selected"] == 1 and result.counters["processed"] == 1
    assert len(env.fake.embedder.texts) == sent + 1  # only the changed text is embedded again
    p1 = env.match("P1")
    assert p1.status == "auto_matched" and p1.antalis_product_id == env.ids["A2"]
    assert p1.deprecated_reason is None and p1.previous_status is None
    assert {r["code"] for r in p1.decision_reasons} >= {"previous_association_changed"}
    assert env.events("P1") == ["computed", "deprecated", "recomputed"]
    with transaction(env.factory) as session:
        event = session.scalar(
            select(ProductMatchEvent).where(
                ProductMatchEvent.event == "deprecated", ProductMatchEvent.product_id == p1.product_id
            )
        )
        assert event.data["antalis_product_id"] == env.ids["A1"] and event.data["status"] == "auto_matched"


def test_human_decisions_survive_reruns_until_the_data_changes(env):
    env.run()
    with transaction(env.factory) as session:  # P2: the reviewer picks the white tape
        match = service.validate_match(
            session, env.ids["P2"], env.fake.config, antalis_product_id=env.ids["A5"], decided_by="alice", comment="ok"
        )
        assert match.status == "validated" and match.manual_choice is (
            match.proposed_antalis_product_id != env.ids["A5"]
        )
    with transaction(env.factory) as session:  # P3: confirmed without equivalent
        service.reject_match(session, env.ids["P3"], env.fake.config, decided_by="bob")
    env.run("all")
    env.run(runtime=Runtime(make_config(thresholds={"auto_match": 95, "min_gap": 8, "review": 75})).runtime)
    assert env.statuses()["P2"] == "validated" and env.statuses()["P3"] == "rejected"
    assert env.match("P2").antalis_product_id == env.ids["A5"] and env.match("P2").decided_by == "alice"

    # the chosen Antalis product changes: the validation becomes obsolete, nothing is lost
    env.update_product(
        "A5",
        title="Ruban adhésif polypropylène blanc 50 mm x 100 m",
        normalized_attributes={"width_mm": 50, "roll_length_m": 100, "color": "Blanc"},
    )
    with transaction(env.factory) as session:
        antalis_id = session.scalar(select(Competitor.id).where(Competitor.key == "antalis_fr"))
        deprecated = service.detect_stale_matches(session, [antalis_id])  # what a sync of Antalis does
    assert [(d.reason, d.previous_status) for d in deprecated] == [("antalis_product_changed", "validated")]
    note = env.notifications("matches_deprecated")[-1]
    assert note.data["by_previous_status"] == {"validated": 1} and "1 human decision" in note.message
    env.run()
    assert env.statuses()["P2"] in ("needs_review", "auto_matched")
    assert env.events("P2") == ["computed", "validated", "deprecated", "recomputed"]


def test_rejected_candidates_are_never_proposed_again(env):
    env.run()
    first = env.match("P2").proposed_antalis_product_id
    with transaction(env.factory) as session:
        rejected = service.reject_match(session, env.ids["P2"], env.fake.config, comment="wrong colour")
        assert rejected.rejected_antalis_product_ids == [first] and rejected.antalis_product_id is None
    with transaction(env.factory) as session:
        reset = service.reset_match(session, env.ids["P2"], env.fake.config, decided_by="alice")
        assert reset.status == "deprecated" and reset.deprecated_reason == "reset_requested"
    env.run("deprecated")
    again = env.match("P2")
    assert again.proposed_antalis_product_id not in (None, first)
    assert env.events("P2") == ["computed", "rejected", "reset", "recomputed"]
    with transaction(env.factory) as session:
        excluded = session.scalar(
            select(ProductMatchCandidate).where(
                ProductMatchCandidate.match_id == again.id, ProductMatchCandidate.antalis_product_id == first
            )
        )
        assert excluded.excluded and excluded.details["exclusions"][-1]["code"] == "rejected_by_human"


def test_missing_or_deleted_reference_products_deprecate_associations(env):
    env.run()
    env.update_product("A1", status="missing")
    with transaction(env.factory) as session:
        reasons = {d.product_id: d.reason for d in service.detect_stale_matches(session)}
    assert reasons == {env.ids["P1"]: "antalis_product_missing"}
    env.run("deprecated")
    with transaction(env.factory) as session:
        session.delete(session.get(Product, env.ids["A6"]))
    with transaction(env.factory) as session:
        reasons = {d.product_id: d.reason for d in service.detect_stale_matches(session)}
    assert reasons == {env.ids["P4"]: "antalis_product_deleted"}
    assert env.match("P4").previous_status == "auto_matched"


def test_a_decision_taken_during_a_run_is_not_overwritten(env):
    env.run()
    env.update_product("P2", description="Adhésif d'emballage, nouveau texte")
    factory, ids, config = env.factory, env.ids, env.fake.config

    class DecideDuringRun(RunObserver):
        def progress(self, step, done, goal=None, pending=None):
            if step == "match" and done == 0:
                with transaction(factory) as session:
                    service.validate_match(session, ids["P2"], config, antalis_product_id=ids["A4"], decided_by="eve")

    result = env.run(observer=DecideDuringRun())
    assert result.counters["skipped_concurrent_change"] == 1
    assert env.match("P2").status == "validated" and env.match("P2").decided_by == "eve"


def test_decisions_are_validated(env):
    env.run()
    config = env.fake.config
    with transaction(env.factory) as session, pytest.raises(MatchingError) as error:
        service.validate_match(session, env.ids["A1"], config)
    assert error.value.code == "matching_reference"
    with transaction(env.factory) as session, pytest.raises(MatchingError) as error:
        service.validate_match(session, env.ids["P1"], config, antalis_product_id=env.ids["P2"])
    assert error.value.code == "invalid_antalis_product"
    with transaction(env.factory) as session, pytest.raises(MatchingError) as error:
        service.validate_match(session, env.ids["P3"], config)  # every candidate excluded: nothing proposed
    assert error.value.code == "antalis_product_required"


def test_summary_and_estimate(env):
    from competitor_catalog.core.config import CompetitorConfig
    from competitor_catalog.services.estimates import estimate_job

    competitor_config = CompetitorConfig("packdiscount_fr", "P", "https://x", "a:b", [], matching=PACKDISCOUNT_OPTIONS)
    with transaction(env.factory) as session:
        estimate = estimate_job(
            session,
            env.competitor_id,
            competitor_config,
            "matching",
            {"mode": "incremental"},
            matching_config=env.fake.config,
        )
        assert estimate.products == 4 and estimate.embeddings == 4 + 7 and estimate.requests == 1
    env.run()
    with transaction(env.factory) as session:
        competitor = session.get(Competitor, env.competitor_id)
        data = service.summary(session, competitor, env.fake.runtime)
        assert data["counts"]["auto_matched"] == 2 and data["needs_review"] == 1 and data["never_matched"] == 0
        assert data["embedding_tokens"] > 0 and data["outdated_by_config"] == 0
        estimate = estimate_job(
            session,
            env.competitor_id,
            competitor_config,
            "matching",
            {"mode": "incremental"},
            matching_config=env.fake.config,
        )
        assert estimate.products == 0 and estimate.embeddings == 0 and estimate.requests == 0


def test_a_catalog_sync_checks_the_matches_of_the_synced_products(env):
    from competitor_catalog.db.models import WorkerJob
    from competitor_catalog.services import jobs

    env.run()
    env.update_product("A6", description="Film étirable, manuel, 23 µ, nouveau film")
    with transaction(env.factory) as session:
        antalis_id = session.scalar(select(Competitor.id).where(Competitor.key == "antalis_fr"))
        job = WorkerJob(competitor_id=antalis_id, task="catalog_sync", status="succeeded")
        session.add(job)
        session.flush()
        job_id = job.id
    jobs.detect_matching_changes(env.factory, job_id)
    assert env.statuses()["P4"] == "deprecated" and env.match("P4").deprecated_reason == "antalis_product_changed"
    note = env.notifications("matches_deprecated")[-1]
    assert note.job_id == job_id and note.competitor_id == env.competitor_id
