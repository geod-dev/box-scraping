"""Matching of competitor products with the reference catalog (Antalis).

* :mod:`.config` - ``config/matching.yaml`` (weights, thresholds, taxonomy);
* :mod:`.text` - what the engine reads of a product, and the hashes that detect changes;
* :mod:`.dimensions`, :mod:`.features`, :mod:`.lexical` - the comparison signals;
* :mod:`.embeddings` - Voyage AI client and the embedding cache;
* :mod:`.engine` - pure scoring and decision (no database);
* :mod:`.service` - database orchestration: runs, change detection, human decisions.
"""
