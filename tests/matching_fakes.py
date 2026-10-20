"""Deterministic stand-in for the Voyage embedder, and a small realistic catalog.

Like voyage-4 on real products (calibration probe, config/matching.yaml), the fake embedding
tells product families apart (cosine ~0.1 between families) but barely separates two sizes
of the same product (cosine ~0.98): the dimensions must do that work.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import numpy as np
from sqlalchemy import select

from competitor_catalog.core.normalize import fold
from competitor_catalog.db.models import Competitor, Product
from competitor_catalog.matching.config import EmbeddingSettings, MatchingConfig, load_matching_config
from competitor_catalog.matching.embeddings import EmbeddingResult, normalize_rows
from competitor_catalog.matching.service import MatchingRuntime
from competitor_catalog.settings import PROJECT_ROOT

DIMENSIONS = 64
FAMILIES = ("caisse", "ruban", "film", "machine", "banc", "devidoir")


def _seed_vector(key: str) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "little")
    return np.random.default_rng(seed).normal(size=DIMENSIONS)


class FakeEmbedder:
    def __init__(self, settings: EmbeddingSettings):
        self.settings = settings
        self.calls: list[list[str]] = []

    @property
    def texts(self) -> list[str]:
        return [t for call in self.calls for t in call]

    def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            folded = fold(text.split("\n")[0])
            family = next((f for f in FAMILIES if f in folded), "other")
            words = [w for w in folded.split() if w.isalpha()]
            noise = sum((_seed_vector(w) for w in words), np.zeros(DIMENSIONS)) / max(1, len(words))
            vectors.append(_seed_vector(f"family:{family}") * 6 + noise)
        return EmbeddingResult(normalize_rows(np.array(vectors)), tokens=sum(len(t) // 4 for t in texts))


def make_config(**overrides) -> MatchingConfig:
    config = load_matching_config(PROJECT_ROOT / "config" / "matching.yaml")
    raw = {**config.raw, "embedding": {**config.raw["embedding"], "dimensions": DIMENSIONS}, **overrides}
    from competitor_catalog.matching.config import parse_matching_config

    return parse_matching_config(raw)


class Runtime:
    """A matching runtime whose embedder is a :class:`FakeEmbedder` (kept for assertions)."""

    def __init__(self, config: MatchingConfig | None = None):
        self.config = config or make_config()
        self.embedder = FakeEmbedder(self.config.embedding)
        self.runtime = MatchingRuntime(self.config, lambda: self.embedder)


NOW = datetime(2026, 10, 20, tzinfo=UTC)

# reference (Antalis) products: (reference, title, description, normalized attributes)
REFERENCE = [
    (
        "A1",
        "Caisse carton simple cannelure 305 x 215 x 105 mm",
        "Caisse américaine, simple cannelure, Kraftliner, brun, paquet de 25 pièces",
        {"inner_length_mm": 305, "inner_width_mm": 215, "inner_height_mm": 105, "color": "Brun"},
    ),
    (
        "A2",
        "Caisse carton simple cannelure 305 x 215 x 170 mm",
        "Caisse américaine, simple cannelure, Kraftliner, brun, paquet de 25 pièces",
        {"inner_length_mm": 305, "inner_width_mm": 215, "inner_height_mm": 170, "color": "Brun"},
    ),
    (
        "A3",
        "Caisse carton double cannelure 305 x 215 x 105 mm",
        "Caisse américaine, double cannelure, Kraftliner, brun, paquet de 15 pièces",
        {"inner_length_mm": 305, "inner_width_mm": 215, "inner_height_mm": 105, "color": "Brun"},
    ),
    (
        "A4",
        "Ruban adhésif polypropylène brun 50 mm x 66 m",
        "Ruban adhésif PP, 28 µ, colle acrylique, brun, carton de 36 rouleaux",
        {"width_mm": 50, "roll_length_m": 66, "color": "Brun"},
    ),
    (
        "A5",
        "Ruban adhésif polypropylène blanc 50 mm x 66 m",
        "Ruban adhésif PP, 28 µ, colle acrylique, blanc, carton de 36 rouleaux",
        {"width_mm": 50, "roll_length_m": 66, "color": "Blanc"},
    ),
    (
        "A6",
        "Film étirable manuel 450 mm x 300 m 17 µ",
        "Film étirable, manuel, 17 µ, 450 mm x 300 m, transparent, carton de 6 rouleaux",
        {"width_mm": 450, "roll_length_m": 300, "thickness_um": 17},
    ),
    (
        "A7",
        "Machine à fermer les caisses Siat SK20",
        "Machine à fermer les caisses, Siat, SK20, semi-automatique, ruban adhésif 50 mm",
        {},
    ),
]

# competitor (Packdiscount) products: (reference, title, description, attributes, expected status, expected ref)
COMPETITOR = [
    (
        "P1",
        "Caisse carton simple cannelure 30,5 x 21,5 x 10,5 cm",
        "➤ Voir toutes les références",
        {"inner_length_mm": 305, "inner_width_mm": 215, "inner_height_mm": 105},
        "auto_matched",
        "A1",
    ),
    ("P2", "Ruban adhésif PP 50 mm x 66 m", "Adhésif d'emballage silencieux", {}, "needs_review", None),
    ("P3", "Socle banc L. 600 mm", "Pour vestiaire industriel", {}, "no_match", None),
    (
        "P4",
        "Film étirable manuel 45 cm x 300 m 17 microns",
        "Film palette transparent, colis de 6 bobines",
        {},
        "auto_matched",
        "A6",
    ),
]


def add_competitor(session, key: str, name: str, *, catalog: bool = True) -> Competitor:
    """Get (e.g. created from config/competitors by the API) or create a competitor."""
    competitor = session.scalar(select(Competitor).where(Competitor.key == key))
    if competitor is None:
        competitor = Competitor(
            key=key,
            name=name,
            base_url=f"https://{key}.example",
            adapter="tests.toy_adapter:ToyAdapter",
            config={"matching": {"ignore_description_patterns": ["^\\W*Voir toutes les r[ée]f[ée]rences"]}}
            if key != "antalis_fr"
            else {},
        )
        session.add(competitor)
    competitor.last_full_catalog_at = NOW if catalog else None
    session.flush()
    return competitor


def add_product(session, competitor: Competitor, reference: str, title: str, description: str, attrs: dict) -> Product:
    product = Product(
        competitor_id=competitor.id,
        identity_key=f"ref:{reference}",
        catalog_reference=reference,
        url=f"{competitor.base_url}/p/{reference}",
        title=title,
        description=description,
        normalized_attributes=attrs,
        status="active",
        details_updated_at=NOW,
    )
    session.add(product)
    session.flush()
    return product


def seed_catalogs(session, *, competitor_catalog: bool = True, reference_catalog: bool = True) -> dict[str, int]:
    """Antalis + Packdiscount with the products above; returns reference -> product id."""
    antalis = add_competitor(session, "antalis_fr", "Antalis France", catalog=reference_catalog)
    packdiscount = add_competitor(session, "packdiscount_fr", "Packdiscount France", catalog=competitor_catalog)
    ids = {}
    for reference, title, description, attrs in REFERENCE:
        ids[reference] = add_product(session, antalis, reference, title, description, attrs).id
    for reference, title, description, attrs, *_expected in COMPETITOR:
        ids[reference] = add_product(session, packdiscount, reference, title, description, attrs).id
    return ids
