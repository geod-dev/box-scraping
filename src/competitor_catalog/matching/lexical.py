"""Lexical similarity: TF-IDF cosine over the words of the reference catalog.

Sub-linear term frequency, smoothed IDF learnt on the reference catalog. A query word unknown
to the reference catalog still counts in the query norm (with the highest IDF), so a text full
of words the reference never uses gets a lower similarity.
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
from scipy import sparse


class LexicalIndex:
    def __init__(self, documents: list[list[str]]):
        self.size = len(documents)
        self.vocabulary: dict[str, int] = {}
        document_frequency: Counter[str] = Counter()
        for tokens in documents:
            document_frequency.update(set(tokens))
        for token in sorted(document_frequency):
            self.vocabulary[token] = len(self.vocabulary)
        self.idf = np.array(
            [math.log((1 + self.size) / (1 + document_frequency[t])) + 1 for t in self.vocabulary], dtype=float
        )
        self.unknown_idf = math.log(1 + self.size) + 1
        rows, cols, values = [], [], []
        for row, tokens in enumerate(documents):
            weights = self._weights(tokens)
            norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
            for token, weight in weights.items():
                rows.append(row)
                cols.append(self.vocabulary[token])
                values.append(weight / norm)
        self.matrix = sparse.csc_matrix(
            (values, (rows, cols)), shape=(self.size, max(1, len(self.vocabulary))), dtype=float
        )
        self.rows = self.matrix.tocsr()  # row access (shared terms of one candidate)

    def _idf(self, token: str) -> float:
        index = self.vocabulary.get(token)
        return float(self.idf[index]) if index is not None else self.unknown_idf

    def _weights(self, tokens: list[str]) -> dict[str, float]:
        return {t: (1 + math.log(n)) * self._idf(t) for t, n in Counter(tokens).items()}

    def query(self, tokens: list[str]) -> dict[str, float]:
        """Normalized query weights (unknown words included in the norm)."""
        weights = self._weights(tokens)
        norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        return {t: w / norm for t, w in weights.items()}

    def scores(self, tokens: list[str]) -> np.ndarray:
        """Cosine similarity of the query with every reference document, shape ``(size,)``."""
        weights = {t: w for t, w in self.query(tokens).items() if t in self.vocabulary}
        if not weights or self.size == 0:
            return np.zeros(self.size)
        columns = [self.vocabulary[t] for t in weights]
        return np.asarray(self.matrix[:, columns] @ np.array(list(weights.values()))).ravel()

    def shared_terms(self, tokens: list[str], row: int, limit: int = 10) -> list[str]:
        """Words shared with one reference document, by decreasing contribution."""
        weights = self.query(tokens)
        start, end = self.rows.indptr[row], self.rows.indptr[row + 1]
        reverse = {self.vocabulary[t]: t for t in weights if t in self.vocabulary}
        contributions = [
            (weights[reverse[col]] * value, reverse[col])
            for col, value in zip(self.rows.indices[start:end], self.rows.data[start:end], strict=True)
            if col in reverse
        ]
        return [t for _c, t in sorted(contributions, reverse=True)[:limit]]
