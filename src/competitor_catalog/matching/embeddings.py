"""Embeddings: the Voyage AI client and the per-product cache (``product_embeddings``).

An embedding is reused as long as the text hash (model + dimensions + input type + text) of
the product is unchanged, so a rerun only pays for the products whose title/description
changed. Identical texts are embedded once per batch. Each batch is committed as soon as it
is computed: an interrupted run does not lose the tokens already spent.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

import httpx
import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.db.models import ProductEmbedding
from competitor_catalog.db.session import transaction
from competitor_catalog.logging_setup import redact, register_secret
from competitor_catalog.matching.config import EmbeddingSettings
from competitor_catalog.matching.text import text_hash
from competitor_catalog.services.runs import utcnow

log = logging.getLogger(__name__)

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


class EmbeddingError(RuntimeError):
    """The embedding provider refused the request (bad key, invalid input...) or stayed down."""


@dataclass
class EmbeddingResult:
    vectors: np.ndarray  # (n, dimensions) float32, L2-normalized
    tokens: int


class Embedder(Protocol):
    settings: EmbeddingSettings

    def embed(self, texts: list[str]) -> EmbeddingResult: ...


def normalize_rows(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


def estimate_tokens(text: str) -> int:
    """Conservative estimate: French product text measured at ~2.75 characters per voyage-4
    token (digits, units and accents tokenize finely)."""
    return int(len(text) / 2.5) + 1


class RateLimiter:
    """Client-side requests/tokens per minute budget (sliding 60 s window), so the account's
    rate limits are respected instead of being hit and retried."""

    WINDOW = 60.0

    def __init__(
        self,
        requests_per_minute: int,
        tokens_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute
        self.clock = clock
        self.sleep = sleep
        self.events: list[list[float]] = []  # [timestamp, tokens]

    def acquire(self, tokens: int) -> None:
        while True:
            now = self.clock()
            self.events = [e for e in self.events if now - e[0] < self.WINDOW]
            used = sum(e[1] for e in self.events)
            if not self.events or (
                len(self.events) < self.requests_per_minute and used + tokens <= self.tokens_per_minute
            ):
                self.events.append([now, tokens])
                return
            self.sleep(max(0.05, self.events[0][0] + self.WINDOW - now))

    def settle(self, tokens: int) -> None:
        """Replace the estimate of the last request with the tokens actually billed."""
        if self.events:
            self.events[-1][1] = tokens


class VoyageEmbedder:
    def __init__(
        self,
        api_key: str,
        settings: EmbeddingSettings,
        *,
        timeout: float = 60.0,
        max_attempts: int = 6,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not api_key:
            raise EmbeddingError("VOYAGE_API_KEY is not set")
        register_secret(api_key)
        self.settings = settings
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.limiter = RateLimiter(settings.requests_per_minute, settings.tokens_per_minute, clock=clock, sleep=sleep)
        self.client = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def close(self) -> None:
        self.client.close()

    def embed(self, texts: list[str]) -> EmbeddingResult:
        payload = {
            "input": texts,
            "model": self.settings.model,
            "input_type": self.settings.input_type,
            "output_dimension": self.settings.dimensions,
            "truncation": True,
        }
        estimate = sum(estimate_tokens(t) for t in texts)
        last_error = "no attempt"
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.acquire(estimate)
            try:
                response = self.client.post(VOYAGE_URL, json=payload, headers=self._headers)
            except httpx.TransportError as exc:  # dropped connection, timeout: retry
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    result = self._parse(response.json(), len(texts))
                    self.limiter.settle(result.tokens)
                    return result
                detail = redact(response.text[:300])
                if response.status_code in (401, 403):
                    raise EmbeddingError(f"Voyage API key rejected (HTTP {response.status_code}): {detail}")
                if response.status_code not in (408, 429) and response.status_code < 500:
                    raise EmbeddingError(f"Voyage API error HTTP {response.status_code}: {detail}")
                last_error = f"HTTP {response.status_code}: {detail}"
                retry_after = response.headers.get("retry-after")
                if retry_after and retry_after.isdigit() and attempt < self.max_attempts:
                    self.sleep(min(60.0, float(retry_after)))
                    continue
            if attempt < self.max_attempts:
                delay = min(60.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                if last_error.startswith("HTTP 429"):  # rate limited: wait for the window to clear
                    delay = max(delay, 60.0 / max(1, self.settings.requests_per_minute))
                log.warning(
                    "Voyage request failed (%s), retry %d/%d in %.1fs", last_error, attempt, self.max_attempts, delay
                )
                self.sleep(delay)
        raise EmbeddingError(f"Voyage API unavailable after {self.max_attempts} attempts: {last_error}")

    def _parse(self, body: dict, expected: int) -> EmbeddingResult:
        items = sorted(body.get("data") or [], key=lambda d: d.get("index", 0))
        if len(items) != expected:
            raise EmbeddingError(f"Voyage returned {len(items)} embeddings for {expected} texts")
        vectors = normalize_rows(np.array([item["embedding"] for item in items], dtype=np.float32))
        if vectors.shape[1] != self.settings.dimensions:
            raise EmbeddingError(f"Voyage returned {vectors.shape[1]} dimensions, expected {self.settings.dimensions}")
        return EmbeddingResult(vectors, int((body.get("usage") or {}).get("total_tokens") or 0))


def to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4").astype(np.float32)


@dataclass
class EmbeddingStats:
    reused: int = 0
    computed: int = 0  # products whose embedding was (re)computed
    texts_sent: int = 0  # distinct texts sent to the provider
    tokens: int = 0
    requests: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "embeddings_reused": self.reused,
            "embeddings_computed": self.computed,
            "embedding_texts_sent": self.texts_sent,
            "embedding_tokens": self.tokens,
            "embedding_requests": self.requests,
        }


def load_cached(
    session: Session, product_ids: Iterable[int], settings: EmbeddingSettings
) -> dict[int, tuple[str, np.ndarray]]:
    """product id -> (text hash, vector) for the cached embeddings of the configured model."""
    ids = list(product_ids)
    result: dict[int, tuple[str, np.ndarray]] = {}
    for start in range(0, len(ids), 2000):
        rows = session.execute(
            select(ProductEmbedding.product_id, ProductEmbedding.text_hash, ProductEmbedding.embedding).where(
                ProductEmbedding.product_id.in_(ids[start : start + 2000]),
                ProductEmbedding.model == settings.model,
                ProductEmbedding.dimensions == settings.dimensions,
            )
        )
        for product_id, hash_, blob in rows:
            result[product_id] = (hash_, from_blob(blob))
    return result


class EmbeddingCache:
    def __init__(self, factory: sessionmaker[Session], embedder: Embedder):
        self.factory = factory
        self.embedder = embedder
        self.settings = embedder.settings

    def ensure(
        self,
        texts: dict[int, str],
        *,
        progress: Callable[[int, int], None] | None = None,
        check_stop: Callable[[], None] | None = None,
        stats: EmbeddingStats | None = None,
    ) -> dict[int, np.ndarray]:
        """Vectors of every product id of ``texts`` (product id -> matching text), computing only
        the missing or outdated ones."""
        stats = stats if stats is not None else EmbeddingStats()
        hashes = {pid: text_hash(text, self.settings) for pid, text in texts.items()}
        with transaction(self.factory) as session:
            cached = load_cached(session, texts, self.settings)
        vectors: dict[int, np.ndarray] = {}
        pending: dict[str, list[int]] = {}  # text hash -> products sharing that text
        for pid, hash_ in hashes.items():
            hit = cached.get(pid)
            if hit is not None and hit[0] == hash_:
                vectors[pid] = hit[1]
                stats.reused += 1
            else:
                pending.setdefault(hash_, []).append(pid)
        # identical text already cached on another product: copy it, no API call
        by_hash = {h: v for h, v in cached.values()}
        copied = [(h, pids) for h, pids in pending.items() if h in by_hash]
        if copied:
            self._store([(pid, h, by_hash[h], None) for h, pids in copied for pid in pids])
            for h, pids in copied:
                for pid in pids:
                    vectors[pid] = by_hash[h]
                del pending[h]
            stats.reused += sum(len(pids) for _h, pids in copied)
        batches = self._batches([(h, texts[pids[0]]) for h, pids in pending.items()])
        total = sum(len(pids) for pids in pending.values())
        done = 0
        if progress:
            progress(done, total)
        for batch in batches:
            if check_stop:
                check_stop()
            result = self.embedder.embed([text for _h, text in batch])
            stats.requests += 1
            stats.tokens += result.tokens
            stats.texts_sent += len(batch)
            rows = []
            batch_chars = max(1, sum(len(t) for _h, t in batch))
            for (hash_, text), vector in zip(batch, result.vectors, strict=True):
                pids = pending[hash_]
                # tokens are billed per request: attribute them to texts pro rata of their length
                share = round(result.tokens * len(text) / batch_chars)
                for i, pid in enumerate(pids):
                    rows.append((pid, hash_, vector, share if i == 0 else None))
                    vectors[pid] = vector
            self._store(rows)
            stats.computed += len(rows)
            done += len(rows)
            if progress:
                progress(done, total)
        return vectors

    def _batches(self, items: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
        batches: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        tokens = 0
        for item in items:
            cost = estimate_tokens(item[1])
            budget = self.settings.batch_token_budget
            if current and (len(current) >= self.settings.batch_size or tokens + cost > budget):
                batches.append(current)
                current, tokens = [], 0
            current.append(item)
            tokens += cost
        if current:
            batches.append(current)
        return batches

    def _store(self, rows: list[tuple[int, str, np.ndarray, int | None]]) -> None:
        if not rows:
            return
        now = utcnow()
        values = [
            {
                "product_id": pid,
                "model": self.settings.model,
                "dimensions": self.settings.dimensions,
                "text_hash": hash_,
                "tokens": tokens,
                "embedding": to_blob(vector),
                "updated_at": now,
            }
            for pid, hash_, vector, tokens in rows
        ]
        stmt = insert(ProductEmbedding).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["product_id"],
            set_={
                c: stmt.excluded[c] for c in ("model", "dimensions", "text_hash", "tokens", "embedding", "updated_at")
            },
        )
        with transaction(self.factory) as session:
            session.execute(stmt)
