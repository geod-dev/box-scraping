"""What the matching engine reads of a product, and the hashes used to detect changes.

Two hashes with different purposes:

* :func:`product_hash` covers the *raw* product fields the engine reads (title,
  descriptions, category name, normalized attributes). It is stored on each match: when it
  differs from the product's current hash, the match is obsolete (``deprecated``). It does
  not depend on any configuration, so editing the config never deprecates human decisions.
* :func:`text_hash` covers the exact text sent to the embedding model (plus the model):
  embeddings are reused as long as it is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from competitor_catalog.core.normalize import clean_text, fold
from competitor_catalog.matching.config import EmbeddingSettings

HASH_VERSION = "1"

_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a au aux avec ce ces d dans de des du en et l la le les ou par pour sans sur un une x mm cm m mx g kg "
    "l ml cl lot pcs pc piece pieces unite unites the and with for".split()
)


@dataclass(frozen=True)
class ProductSource:
    """The product fields the engine reads (one row of ``products`` + its category name)."""

    product_id: int
    competitor_id: int
    status: str
    title: str | None
    description: str | None
    long_description: str | None
    category: str | None
    normalized_attributes: dict[str, Any] | None
    reference: str | None = None
    url: str | None = None


def product_hash(source: ProductSource) -> str:
    payload = {
        "v": HASH_VERSION,
        "title": source.title,
        "description": source.description,
        "long_description": source.long_description,
        "category": source.category,
        "attributes": source.normalized_attributes or {},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class TextOptions:
    """Per-competitor text cleaning, from the ``matching`` section of the competitor config."""

    ignore_description_patterns: tuple[re.Pattern[str], ...] = ()
    max_description_chars: int = 1500

    @classmethod
    def from_options(cls, options: dict[str, Any] | None, max_description_chars: int = 1500) -> TextOptions:
        options = options or {}
        patterns = tuple(re.compile(p, re.IGNORECASE) for p in options.get("ignore_description_patterns") or [])
        return cls(patterns, max_description_chars)


def effective_description(source: ProductSource, options: TextOptions) -> str | None:
    """The short description, else the long one, skipping boilerplate (e.g. Packdiscount
    variants: "➤ Voir toutes les références") and descriptions that only repeat the title."""
    title = fold(source.title or "")
    for candidate in (source.description, source.long_description):
        text = clean_text(candidate)
        if not text or fold(text) == title:
            continue
        if any(p.search(text) for p in options.ignore_description_patterns):
            continue
        return text[: options.max_description_chars]
    return None


def _category_line(source: ProductSource) -> str | None:
    category = clean_text(source.category)
    if not category or fold(category) in fold(source.title or ""):
        return None
    return category


def embedding_text(source: ProductSource, options: TextOptions) -> str:
    """Title + description (the requested signal), plus the category name, which carries the
    product family when the description is empty (frequent for product variants)."""
    lines = [clean_text(source.title) or ""]
    description = effective_description(source, options)
    if description:
        lines.append(description)
    category = _category_line(source)
    if category:
        lines.append(f"Catégorie : {category}")
    return "\n".join(line for line in lines if line)


def lexical_text(source: ProductSource, options: TextOptions) -> str:
    title = clean_text(source.title) or ""
    # The title is repeated: its words weigh twice those of the description.
    parts = [title, title, effective_description(source, options) or "", _category_line(source) or ""]
    return "\n".join(parts)


def text_hash(text: str, settings: EmbeddingSettings) -> str:
    key = f"{settings.model}|{settings.dimensions}|{settings.input_type}|{text}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _singular(token: str) -> str:
    return token[:-1] if len(token) > 3 and token[-1] in "sx" and not token.endswith("ss") else token


def tokens(text: str) -> list[str]:
    """Folded words without stop words and without pure numbers (dimensions are compared
    numerically, and "300" in mm vs "30" in cm would only add noise)."""
    return [_singular(t) for t in _TOKEN.findall(fold(text)) if len(t) > 1 and not t.isdigit() and t not in STOPWORDS]


@lru_cache(maxsize=4096)
def keyword_regex(keyword: str) -> re.Pattern[str]:
    """Whole-word match of a folded keyword; spaces and hyphens are interchangeable and a
    final s/x is tolerated."""
    parts = [re.escape(p) for p in re.split(r"[\s-]+", keyword.strip()) if p]
    return re.compile(r"(?<![a-z0-9])" + r"[\s-]+".join(parts) + r"(?:s|x)?(?![a-z0-9])")


def find_keyword(text_folded: str, keyword: str) -> int | None:
    match = keyword_regex(keyword).search(text_folded)
    return match.start() if match else None
