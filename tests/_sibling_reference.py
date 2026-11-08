"""Frozen production methods from main 37c686eb1c1ee89f31f91b71d133b25a772f7011 for differential evaluation."""
from raggio.store import HYBRID_DEPTH, _no_leg, _or_query, _relative, _rrf, asyncio, np

BASELINE_REVISION = '37c686eb1c1ee89f31f91b71d133b25a772f7011'
BASELINE_STORE_SHA256 = '7b593115c86eac7fed632bef8a7ca5ac098bb398bbcf5fb1bae0669c7e4bb399'

def _search_allowed(
    self, qvec: np.ndarray, n: int, allow: np.ndarray, **kw
) -> tuple[list[int], list[float]]:
    """Top-n of one query over an allowlist (filters, sibling expansion). An
    allowlist no larger than the rescore depth skips the index (ADR 0007): the
    masked search would return all of it and the rescore fetch every blob anyway,
    so scoring the fp16 originals directly gives the same ids, scores and
    (-score, id) order without the index call. Past that depth a masked search's
    bounded fetch is cheaper (each blob costs ~20 us at 3072-d). Blocking; call
    from a thread."""
    if len(allow) <= self._rescore_k(n):
        ids, mat = self._fp16_vecs(allow.tolist())
        if len(ids) == len(allow):  # else rows without a blob: only the index scores them
            scores = mat @ qvec[0]
            top = np.lexsort((ids, -scores))[:n]
            return [ids[j] for j in top], [float(scores[j]) for j in top]
    return self._search_rescored(qvec, [n], allowlist=allow, **kw)[0]

async def search(
    self,
    mode: str,
    qvec: np.ndarray | None,
    qtext: str | None,
    k: int,
    scope: str,
    filt: dict | None,
    expand,
    nprobe: int | None = None,
    fusion: str = "rrf",
    alpha: float = 0.5,
) -> list[dict]:
    n = max(HYBRID_DEPTH, k) if mode == "hybrid" else k  # per-leg depth so fusion sees the tail
    async with self.lock.read():
        if self._closed:  # queued for the read lock behind stop()'s write section
            raise self._closed_error()
        if mode == "vector":
            ids, scores = await self._vector_ids(qvec, n, scope, filt, nprobe)
        elif mode == "text":
            ids, scores = await asyncio.to_thread(self._text_ids, qtext, n, scope, filt)
        else:  # hybrid: legs in parallel — sqlite releases the GIL, so they overlap
            # relative fusion skips a leg whose weight is 0, as Weaviate does
            rrf = fusion == "rrf"
            vec, text = await asyncio.gather(
                self._vector_ids(qvec, n, scope, filt, nprobe) if rrf or alpha > 0 else _no_leg(),
                asyncio.to_thread(self._text_ids, qtext, n, scope, filt) if rrf or alpha < 1 else _no_leg(),
            )
            ids, scores = _rrf(k, vec[0], text[0]) if rrf else _relative(k, alpha, vec, text)
        hits = self._hydrate(ids, scores)
        if expand:
            for hit in hits:
                await self._expand(hit, qvec, qtext, expand)
    return hits

async def _expand(self, hit: dict, qvec: np.ndarray | None, qtext: str | None, expand) -> None:
    doc_id, ext = hit["doc_id"], {}
    self_clause = "AND external_id != ?" if hit["type"] == "chunk" else ""
    self_param = [hit["id"]] if hit["type"] == "chunk" else []
    if expand.siblings_topk and qvec is None:
        # text mode: rank siblings by BM25 (siblings matching no query term are
        # omitted; pruned matching is fine doc-scoped — the match set is tiny)
        match = _or_query(self._prune_common(qtext or "")[0])
        def sibling_rows():
            with self._reading() as db:
                return db.execute(
                    "SELECT r.id, -bm25(records_fts) FROM records_fts"
                    " JOIN records r ON r.id = records_fts.rowid"
                    f" WHERE records_fts MATCH ? AND doc_id=? AND type='chunk' AND indexed=1 {self_clause}"
                    " ORDER BY bm25(records_fts) LIMIT ?",
                    [match, doc_id, *self_param, expand.siblings_topk],
                ).fetchall()

        rows = await asyncio.to_thread(sibling_rows) if match else []
        ext["siblings"] = self._hydrate([r[0] for r in rows], [r[1] for r in rows])
    elif expand.siblings_topk:
        with self._reading() as db:
            sib = [
                r[0]
                for r in db.execute(
                    f"SELECT id FROM records WHERE doc_id=? AND type='chunk' AND indexed=1 {self_clause}",
                    [doc_id, *self_param],
                )
            ]
        if sib:
            allow = np.array(sib, dtype=np.uint64)
            ids, scores = await asyncio.to_thread(
                self._search_allowed, qvec, expand.siblings_topk, allow
            )
            ext["siblings"] = self._hydrate(ids, scores)
        else:
            ext["siblings"] = []
    elif expand.siblings_all:
        with self._reading() as db:
            sib = [
                r[0]
                for r in db.execute(
                    f"SELECT id FROM records WHERE doc_id=? AND type='chunk' {self_clause}"
                    " ORDER BY position, id",
                    [doc_id, *self_param],
                )
            ]
        ext["siblings"] = self._hydrate(sib)
    if expand.summary and hit["type"] != "summary":
        with self._reading() as db:
            row = db.execute(
                "SELECT id FROM records WHERE doc_id=? AND type='summary'", (doc_id,)
            ).fetchone()
        ext["summary"] = self._hydrate([row[0]])[0] if row else None
    if ext:
        hit["expansion"] = ext

METHODS = {"search": search, "_expand": _expand, "_search_allowed": _search_allowed}
