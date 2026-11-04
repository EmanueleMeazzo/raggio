"""Exploration-only normalized sibling-vector reuse; no production cache added."""
from collections import Counter
from contextvars import ContextVar

import numpy as np

from expansion_read_probe import store

_original_fp16_vecs = store.Collection._fp16_vecs
_cache = ContextVar("expansion_probe_vectors", default=None)
VECTOR_BUDGET = 8 * 1024 * 1024


def cached_fp16_vecs(col, ids):
    cached = _cache.get()
    if cached is None or cached[0] is not col or any(rid not in cached[1] for rid in ids):
        return _original_fp16_vecs(col, ids)
    # Small eligible documents use one IN lookup; SQLite visits rowids ascending.
    order = sorted(set(ids))
    return order, np.array([cached[1][rid] for rid in order], dtype=np.float32)


async def vector_expansion(col, hits, qvec, qtext, expand):
    counts = Counter(hit["doc_id"] for hit in hits)
    repeated = [doc for doc, n in counts.items() if n > 1]
    if qvec is None or not expand.siblings_topk or not repeated:
        for hit in hits:
            await col._expand(hit, qvec, qtext, expand)
        return
    with col._reading() as db:
        sizes = dict(store._rows_by_id(db,
            "SELECT doc_id, COUNT(*) FROM records WHERE doc_id IN ({}) AND type='chunk' AND indexed=1 GROUP BY doc_id", repeated))
    depth = col._rescore_k(expand.siblings_topk)
    eligible, remaining = [], VECTOR_BUDGET // (col.cfg.dim * 4)
    # A summary hit doesn't exclude itself; only cache documents whose FULL size
    # stays in the existing exact path. Bigger/missing-vector cases fall back.
    for doc in sorted(repeated, key=lambda doc: -counts[doc]):
        n = sizes.get(doc, 0)
        if 0 < n <= depth and n <= remaining:
            eligible.append(doc)
            remaining -= n
    if not eligible:
        for hit in hits:
            await col._expand(hit, qvec, qtext, expand)
        return
    with col._reading() as db:
        rows = list(store._rows_by_id(db,
            "SELECT id, doc_id FROM records WHERE doc_id IN ({}) AND type='chunk' AND indexed=1", eligible))
    groups = {doc: [] for doc in eligible}
    for rid, doc in rows:
        groups[doc].append(rid)
    wanted = [rid for doc in eligible for rid in groups[doc]]
    # Call the original normalizer once; never cache quantized search results.
    found, mat = _original_fp16_vecs(col, wanted)
    rowof = {rid: mat[n] for n, rid in enumerate(found)}
    valid = {doc for doc, ids in groups.items() if ids and all(rid in rowof for rid in ids)}
    allowed = {rid: rowof[rid] for doc in valid for rid in groups[doc]}
    token = _cache.set((col, allowed))
    try:
        for hit in hits:
            await col._expand(hit, qvec, qtext, expand)
    finally:
        _cache.reset(token)
