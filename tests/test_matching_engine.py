"""Product matching, pure parts: config, text, dimensions, features, lexical, engine, Voyage client."""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from competitor_catalog.matching.config import MatchingConfigError, parse_matching_config
from competitor_catalog.matching.dimensions import DimensionIndex, extract_profile, parse_title
from competitor_catalog.matching.embeddings import EmbeddingCache, EmbeddingError, RateLimiter, VoyageEmbedder
from competitor_catalog.matching.engine import Candidate, MatchEngine, ReferenceIndex, build_doc
from competitor_catalog.matching.features import compare_features, detect_type, extract_features
from competitor_catalog.matching.lexical import LexicalIndex
from competitor_catalog.matching.text import (
    ProductSource,
    TextOptions,
    effective_description,
    embedding_text,
    lexical_text,
    product_hash,
    text_hash,
    tokens,
)
from tests.matching_fakes import COMPETITOR, REFERENCE, FakeEmbedder, make_config

CONFIG = make_config()
PACKDISCOUNT_TEXT = TextOptions.from_options({"ignore_description_patterns": ["^\\W*Voir toutes les r[ée]f[ée]rences"]})


def source(pid, title, description=None, attrs=None, category=None, competitor_id=1, **kw) -> ProductSource:
    return ProductSource(pid, competitor_id, "active", title, description, kw.get("long"), category, attrs)


# --------------------------------------------------------------------------- config


def test_shipped_config_is_valid_and_hash_tracks_changes():
    assert CONFIG.reference_competitor == "antalis_fr"
    assert CONFIG.auto_match_threshold == 90 and CONFIG.min_gap == 8 and CONFIG.review_threshold == 75
    assert CONFIG.certain_threshold == 99
    assert CONFIG.dimension_tolerance == 0.005 and CONFIG.dimension_mismatch_policy == "exclude"
    assert CONFIG.weights_with_dimensions == {"semantic": 0.5, "lexical": 0.2, "dimensions": 0.3}
    assert CONFIG.weights_without_dimensions == {"semantic": 0.75, "lexical": 0.25}
    other = make_config(thresholds={"auto_match": 92, "min_gap": 8, "review": 75})
    assert other.config_hash() != CONFIG.config_hash()
    assert CONFIG.config_hash({"ignore_description_patterns": ["x"]}) != CONFIG.config_hash()


@pytest.mark.parametrize(
    "change",
    [
        {"weights": {"with_dimensions": {"semantic": 0.5, "lexical": 0.2, "dimensions": 0.2}}},
        {"thresholds": {"auto_match": 70, "review": 80}},
        {"thresholds": {"auto_match": 90, "certain": 85, "review": 75}},
        {"dimensions": {"tolerance": 0.005, "mismatch_policy": "maybe"}},
        {"features": {"color": {"policy": "maybe", "values": {"a": ["b"]}}}},
        {"compatible_types": [["box", "spaceship"]]},
    ],
)
def test_invalid_configs_are_refused(change):
    with pytest.raises(MatchingConfigError):
        parse_matching_config({**CONFIG.raw, **change})


# --------------------------------------------------------------------------- text & hashes


def test_boilerplate_description_falls_back_to_long_description_and_category():
    s = source(1, "Caisse carton 30 x 20 x 10 cm", "➤ Voir toutes les références", category="Caisse télescopique")
    assert effective_description(s, PACKDISCOUNT_TEXT) is None
    assert embedding_text(s, PACKDISCOUNT_TEXT) == "Caisse carton 30 x 20 x 10 cm\nCatégorie : Caisse télescopique"
    s2 = source(1, "Caisse", "➤ Voir toutes les références", long="Carton brun très solide")
    assert effective_description(s2, PACKDISCOUNT_TEXT) == "Carton brun très solide"


def test_descriptions_stay_out_of_the_compared_texts_unless_enabled():
    faq = "Peut-on y expédier des objets fragiles ? Oui, avec calage adapté. Empilable et recyclable."
    s = source(1, "Caisse carton simple cannelure 45 x 32 x 17 cm", faq, category="Caisses")
    assert embedding_text(s, TextOptions()) == "Caisse carton simple cannelure 45 x 32 x 17 cm\nCatégorie : Caisses"
    assert "fragile" not in tokens(lexical_text(s, TextOptions()))
    assert not CONFIG.description_in_similarity
    with_description = TextOptions(description_in_similarity=True)
    assert faq in embedding_text(s, with_description)
    assert "fragile" in tokens(lexical_text(s, with_description))
    # Characteristics are still read in the description.
    doc = build_doc(source(2, "Caisse carton", "carton brun"), TextOptions(), CONFIG)
    assert doc.features.values["color"] == frozenset({"brown"})


def test_product_hash_covers_matching_fields_only():
    base = source(1, "Caisse", "desc", {"color": "Brun"}, "Caisses")
    assert product_hash(base) == product_hash(source(1, "Caisse", "desc", {"color": "Brun"}, "Caisses"))
    assert product_hash(base) != product_hash(source(1, "Caisse 2", "desc", {"color": "Brun"}, "Caisses"))
    assert product_hash(base) != product_hash(source(1, "Caisse", "desc", {"color": "Blanc"}, "Caisses"))
    assert product_hash(base) != product_hash(source(1, "Caisse", "desc", {"color": "Brun"}, "Boîtes"))


def test_text_hash_depends_on_the_model():
    other = make_config(embedding={**CONFIG.raw["embedding"], "model": "voyage-4-lite", "dimensions": 64})
    assert text_hash("abc", CONFIG.embedding) != text_hash("abc", other.embedding)


def test_tokens_drop_numbers_stopwords_and_plurals():
    assert tokens("Caisses carton 300 x 200 mm pour l'expédition, RA3") == ["caisse", "carton", "expedition", "ra3"]


# --------------------------------------------------------------------------- dimensions


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Caisse carton 30,5 x 21,5 x 10,5 cm", {"box_mm": (305.0, 215.0, 105.0)}),
        ("Caisse 430 x 305 x 250 mm", {"box_mm": (430.0, 305.0, 250.0)}),
        ("Ruban adhésif 50 mm x 66 m 28 µ", {"width_mm": (50.0,), "length_m": (66.0,), "thickness_um": (28.0,)}),
        (
            "Papier étirable 500 mm x 120 m 48 g/m²",
            {"width_mm": (500.0,), "length_m": (120.0,), "grammage_g_m2": (48.0,)},
        ),
        (
            "Film Retractable Polyolefine l.300mm X L.1000M 19µ",
            {"width_mm": (300.0,), "length_m": (1000.0,), "thickness_um": (19.0,)},
        ),
        ("Enveloppe 229 x 324 mm", {"flat_mm": (229.0, 324.0)}),
        ("Sac poubelle 100 L", {"volume_l": (100.0,)}),
        ("Socle banc L. 600 mm", {}),
        ("Caisse 300 x 200 x 150", {}),  # no unit: not reliable
    ],
)
def test_parse_title(title, expected):
    assert parse_title(title) == expected


def test_profile_prefers_inner_dimensions_then_title_then_outer():
    attrs = {"inner_length_mm": 264, "inner_width_mm": 191, "inner_height_mm": 83, "length_mm": 300}
    assert extract_profile("Caisse 300 x 200 x 85 mm", attrs).values["box_mm"] == (264, 191, 83)
    outer = {"length_mm": 508, "width_mm": 308, "height_mm": 314}
    assert extract_profile("Caisse 500 x 300 x 300 mm", outer).values["box_mm"] == (500, 300, 300)
    assert extract_profile("Caisse", outer).sources["box_mm"] == "attributes"


def test_dimension_scores():
    profiles = [
        extract_profile("Caisse 305 x 215 x 105 mm", None),
        extract_profile("Caisse 305 x 215 x 170 mm", None),
        extract_profile("Caisse 215 x 305 x 105 mm", None),  # same values, another order
        extract_profile("Ruban 50 mm x 66 m", None),
        extract_profile("Caisse", None),
    ]
    index = DimensionIndex(profiles, CONFIG.dimension_tolerance)
    scores = index.score(extract_profile("Caisse 30,5 x 21,5 x 10,5 cm", None))
    assert scores.total[0] == pytest.approx(1.0)  # same box, written in cm
    assert scores.total[1] == 0 and scores.mismatch(1)  # height 38 % off: another box
    assert scores.total[2] == pytest.approx(0.9)  # unordered: penalized
    assert np.isnan(scores.total[3]) and np.isnan(scores.total[4])  # not comparable
    assert not scores.mismatch(3)
    details = index.details(extract_profile("Caisse 305 x 215 x 105 mm", None), scores, 1)
    assert details[0]["key"] == "box_mm" and details[0]["relative_difference"] == pytest.approx(0.3824, abs=1e-3)
    # Not the same numbers, not the same product: 1 mm on 105 mm is already another box...
    assert index.score(extract_profile("Caisse 30,5 x 21,5 x 10,6 cm", None)).total[0] == 0
    # ... while rounding stays tolerated (1 mm on 230 mm).
    flat = DimensionIndex([extract_profile("Enveloppe 229 x 324 mm", None)], CONFIG.dimension_tolerance)
    assert flat.score(extract_profile("Enveloppe 230 x 325 mm", None)).total[0] == 1


def test_a_box_with_close_but_different_dimensions_is_another_product():
    titles = ["Caisse carton double cannelure 780 x 580 x 600 mm", "Caisse carton double cannelure 800 x 600 x 600 mm"]
    embedder = FakeEmbedder(CONFIG.embedding)

    def match(config):
        refs = [build_doc(source(i, t, category="Caisses"), TextOptions(), config) for i, t in enumerate(titles, 1)]
        vectors = embedder.embed([d.embedding_text for d in refs]).vectors
        doc = build_doc(source(9, titles[1], category="Caisses"), TextOptions(), config)
        return MatchEngine(config, ReferenceIndex(refs, vectors, config)).match(
            doc, embedder.embed([doc.embedding_text]).vectors[0]
        )

    outcome = match(CONFIG)
    assert outcome.status == "auto_matched" and outcome.best.product_id == 2 and outcome.score == 100
    other = next(c for c in outcome.candidates if c.product_id == 1)
    assert other.excluded and other.dimensions == 0 and other.rank is None
    assert other.exclusions[0]["code"] == "dimension_mismatch"
    assert "800 × 600 × 600 vs 780 × 580 × 600 mm" in other.exclusions[0]["message"]
    review = match(make_config(dimensions={"tolerance": 0.005, "mismatch_policy": "review"}))
    other = next(c for c in review.candidates if c.product_id == 1)
    assert not other.excluded and other.review_flags[-1]["code"] == "dimension_mismatch"


def _decide(scores):
    engine, _ = _engine()
    candidates = [
        Candidate(i, score, 1.0, 1.0, 1.0, False, [], [], {"semantic": {"cosine": 0.9}, "lexical": {"cosine": 0.9}})
        for i, score in enumerate(scores, 1)
    ]
    return engine._decide(candidates)


def test_identical_candidate_is_automatic_even_with_a_close_second():
    outcome = _decide([100, 96.35])
    assert outcome.status == "auto_matched" and outcome.best.product_id == 1
    warning = next(r for r in outcome.reasons if r["code"] == "close_runner_up")
    assert warning["warning"] is True and "96.35" in warning["message"]
    assert _decide([97, 93]).status == "needs_review"  # below "certain": the gap rule applies
    tie = _decide([100, 99.5])  # two identical candidates: nothing tells them apart
    assert tie.status == "needs_review"
    assert "nothing tells them apart" in next(r for r in tie.reasons if r["code"] == "ambiguous_gap")["message"]


# --------------------------------------------------------------------------- features


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Machine à fermer les caisses Siat SK20", "machine"),
        ("Ruban adhésif pour caisses 50 mm", "tape"),
        ("Dévidoir de ruban adhésif", "dispenser"),
        ("Papier étirable manuel", "stretch_film"),
        ("Papier bulle 1 m x 50 m", "bubble_wrap"),
        ("Carton ondulé simple face", "corrugated_sheet"),
        ("Caisse carton télescopique", "box"),
        ("Pèse colis 30 kg", "scale"),
        ("Bidule SK20", None),
    ],
)
def test_detect_type(title, expected):
    assert detect_type(title, CONFIG) == expected


def test_type_falls_back_to_category_and_feature_conflicts():
    features = extract_features("Modèle XL", None, "Caisse carton simple cannelure", None, CONFIG)
    assert features.product_type == "box" and features.type_source == "category"
    assert features.values["flute_wall"] == {"single"}
    double = extract_features("Caisse double cannelure", "brune", None, None, CONFIG)
    white = extract_features("Caisse simple cannelure", "blanche", None, None, CONFIG)
    brown = extract_features("Caisse simple cannelure", "écrue", None, {"color": "Brun"}, CONFIG)
    assert [e["field"] for e in compare_features(white, double, CONFIG).exclusions] == ["flute_wall"]
    assert [f["field"] for f in compare_features(white, brown, CONFIG).review_flags] == ["color"]
    assert compare_features(brown, brown, CONFIG).review_flags == []
    tape = extract_features("Ruban adhésif PVC", None, None, None, CONFIG)
    box = extract_features("Caisse PVC", None, None, None, CONFIG)
    assert tape.values["tape_material"] == {"pvc"} and "tape_material" not in box.values  # tape-only feature
    assert compare_features(tape, box, CONFIG).exclusions[0]["code"] == "incompatible_product_type"


# --------------------------------------------------------------------------- lexical


def test_lexical_cosine_and_shared_terms():
    index = LexicalIndex([tokens("caisse carton brun"), tokens("ruban adhesif brun"), tokens("film etirable")])
    scores = index.scores(tokens("caisse carton brun"))
    assert scores[0] == pytest.approx(1.0) and scores[1] < 0.3 and scores[2] == 0
    assert index.scores(tokens("caisse carton brun licorne"))[0] < 1  # unknown words lower the similarity
    assert index.shared_terms(tokens("caisse brun"), 0) == ["caisse", "brun"]


# --------------------------------------------------------------------------- engine


def _engine(config=CONFIG):
    embedder = FakeEmbedder(config.embedding)
    refs = [build_doc(source(i, t, d, a), TextOptions(), config) for i, (_r, t, d, a) in enumerate(REFERENCE, 1)]
    vectors = embedder.embed([d.embedding_text for d in refs]).vectors
    return MatchEngine(config, ReferenceIndex(refs, vectors, config)), embedder


def _competitor(i: int, config=CONFIG):
    reference, title, description, attrs, status, expected = COMPETITOR[i]
    return build_doc(source(100 + i, title, description, attrs, competitor_id=2), PACKDISCOUNT_TEXT, config), (
        status,
        expected,
    )


@pytest.mark.parametrize("i", range(len(COMPETITOR)))
def test_engine_decisions_on_the_sample_catalog(i):
    engine, embedder = _engine()
    doc, (status, expected) = _competitor(i)
    outcome = engine.match(doc, embedder.embed([doc.embedding_text]).vectors[0])
    assert outcome.status == status, outcome.reasons
    if expected:
        assert outcome.best.product_id == [r[0] for r in REFERENCE].index(expected) + 1


def test_equivalent_box_is_auto_matched_and_similar_ones_explained():
    engine, embedder = _engine()
    doc, _ = _competitor(0)
    outcome = engine.match(doc, embedder.embed([doc.embedding_text]).vectors[0])
    best = outcome.best
    assert best.product_id == 1 and best.score >= 90 and best.dimensions == pytest.approx(1.0)
    # the other sizes and the double flute box are excluded: no compatible competitor left
    assert {r["code"] for r in outcome.reasons} == {"score_above_auto", "single_candidate"}
    other_size = next(c for c in outcome.candidates if c.product_id == 2)
    assert other_size.dimensions == 0 and other_size.excluded and other_size.score < 75
    assert other_size.details["differences"][0]["field"] == "box_mm"
    double = next(c for c in outcome.candidates if c.product_id == 3)
    assert double.excluded and double.exclusions[0]["field"] == "flute_wall" and double.rank is None
    assert best.details["weights"] == {"semantic": 0.5, "lexical": 0.2, "dimensions": 0.3}
    assert "dimensions" in best.details["retrieved_by"]


def test_ambiguous_candidates_go_to_review_and_rejections_are_excluded():
    engine, embedder = _engine()
    doc, _ = _competitor(1)  # tape: brown and white references are equally close
    vector = embedder.embed([doc.embedding_text]).vectors[0]
    outcome = engine.match(doc, vector)
    assert outcome.status == "needs_review" and outcome.gap < CONFIG.min_gap
    assert "ambiguous_gap" in {r["code"] for r in outcome.reasons}
    rejected = outcome.best.product_id
    again = engine.match(doc, vector, rejected_ids=frozenset({rejected}))
    assert again.best.product_id != rejected
    assert any(c.product_id == rejected and c.exclusions[-1]["code"] == "rejected_by_human" for c in again.candidates)


def test_conflict_and_missing_dimensions_block_the_automatic_association():
    engine, embedder = _engine()
    white = build_doc(source(200, "Ruban adhésif polypropylène blanc 50 mm x 66 m", "blanc"), TextOptions(), CONFIG)
    outcome = engine.match(white, embedder.embed([white.embedding_text]).vectors[0])
    assert outcome.best.product_id == 5  # the white tape, the brown one is flagged
    brown = next(c for c in outcome.candidates if c.product_id == 4)
    assert [f["field"] for f in brown.review_flags if f["code"] == "feature_conflict"] == ["color"]
    sizeless = build_doc(source(201, "Caisse carton simple cannelure", "brune"), TextOptions(), CONFIG)
    outcome = engine.match(sizeless, embedder.embed([sizeless.embedding_text]).vectors[0])
    assert outcome.status != "auto_matched"
    assert "dimensions_not_comparable" in {r["code"] for r in outcome.best.review_flags}
    assert outcome.best.details["weights"] == {"semantic": 0.75, "lexical": 0.25}


def test_score_without_embedding_renormalizes_weights():
    engine, _ = _engine()
    doc, _ = _competitor(0)
    candidate = engine.compare(doc, None, 1)
    assert candidate.semantic is None
    assert candidate.details["weights"] == {"lexical": 0.4, "dimensions": 0.6}


# --------------------------------------------------------------------------- Voyage client


def _voyage(handler, **kw):
    sleeps = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = VoyageEmbedder("pa-test-key-123", CONFIG.embedding, client=client, sleep=sleeps.append, **kw)
    return embedder, sleeps


def test_voyage_request_payload_order_and_normalization():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content), auth=request.headers["authorization"])
        data = [{"index": 1, "embedding": [0.0, 2.0] + [0.0] * 62}, {"index": 0, "embedding": [3.0] + [0.0] * 63}]
        return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 12}})

    embedder, _ = _voyage(handler)
    result = embedder.embed(["a", "b"])
    assert seen["model"] == "voyage-4" and seen["output_dimension"] == 64 and seen["input_type"] is None
    assert seen["auth"] == "Bearer pa-test-key-123" and seen["truncation"] is True
    assert result.tokens == 12 and result.vectors[0][0] == pytest.approx(1.0) and result.vectors[1][1] == 1.0


def test_voyage_retries_transient_errors_and_rejects_bad_keys():
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("dropped")
        if len(calls) == 2:
            return httpx.Response(429, headers={"retry-after": "2"}, text="slow down")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * 64}], "usage": {}})

    embedder, sleeps = _voyage(flaky)
    assert embedder.embed(["x"]).vectors.shape == (1, 64)
    assert len(calls) == 3 and sleeps[1] == 2.0

    embedder, _ = _voyage(lambda r: httpx.Response(401, text="invalid key pa-test-key-123"))
    with pytest.raises(EmbeddingError, match="rejected") as error:
        embedder.embed(["x"])
    assert "pa-test-key-123" not in str(error.value)  # never echoed

    embedder, sleeps = _voyage(lambda r: httpx.Response(503, text="down"), max_attempts=3)
    with pytest.raises(EmbeddingError, match="unavailable after 3 attempts"):
        embedder.embed(["x"])
    assert len(sleeps) == 2


def test_rate_limiter_paces_requests_and_tokens():
    now = [0.0]
    waits = []

    def sleep(seconds):
        waits.append(round(seconds, 2))
        now[0] += seconds

    limiter = RateLimiter(3, 10_000, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.acquire(1000)
    assert waits == []
    limiter.acquire(1000)  # 4th request in the same minute: waits for the first one to expire
    assert waits == [60.0]
    limiter.acquire(9500)  # token budget: waits until enough tokens left the window
    assert sum(waits) > 60 and now[0] >= 60
    limiter.settle(10)
    assert limiter.events[-1][1] == 10


def test_embedding_batches_respect_size_and_token_budget():
    config = make_config(
        embedding={**CONFIG.raw["embedding"], "dimensions": 64, "batch_size": 3, "max_batch_tokens": 50}
    )
    cache = EmbeddingCache(None, FakeEmbedder(config.embedding))
    batches = cache._batches([(str(i), "x" * 60) for i in range(5)] + [(str(i), "y") for i in range(5, 12)])
    # 60 chars ~ 25 estimated tokens, budget 50: two per request; then 3 short texts per request
    assert [len(b) for b in batches] == [2, 2, 3, 3, 2]
    small = make_config(embedding={**CONFIG.raw["embedding"], "dimensions": 64, "tokens_per_minute": 40})
    assert small.embedding.batch_token_budget == 20  # never more than half the minute budget
