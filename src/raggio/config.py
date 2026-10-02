import os


def default_ivf_search_threads() -> int:
    """IVF shard fan-out pool size when IVF_SEARCH_THREADS is unset (ADR 0005). 12 is
    p3's best on the 20-core DGX, chosen on seed 7 and held on seed 42; 20 lost 14-17 %."""
    return min(12, os.cpu_count() or 1)


class Settings:
    def __init__(self) -> None:
        self.root_api_key = os.environ.get("ROOT_API_KEY", "")
        self.embedding_base_url = os.environ.get("EMBEDDING_BASE_URL", "")
        self.embedding_api_key = os.environ.get("EMBEDDING_API_KEY", "")
        self.embedding_model = os.environ.get("EMBEDDING_MODEL", "")
        dim = os.environ.get("EMBEDDING_DIM", "")
        self.embedding_dim = int(dim) if dim else None
        self.data_dir = os.environ.get("DATA_DIR", "/data")
        self.max_resident_collections = int(os.environ.get("MAX_RESIDENT_COLLECTIONS", "4"))
        self.collection_idle_ttl = float(os.environ.get("COLLECTION_IDLE_TTL", "900"))
        # stage-2 BM25 scorer (ADR 0004): "auto" runs raggio_native when it is installed,
        # "0" forces the pure-Python reference (the same results, slower)
        self.native_bm25 = os.environ.get("NATIVE_BM25") or "auto"
        if self.native_bm25 not in ("auto", "0"):
            raise ValueError(f"NATIVE_BM25 must be 'auto' or '0', not {self.native_bm25!r}")
        # per-collection pool for IVF shard searches; 1 = the serial loop, unset/0 = default
        threads = int(os.environ.get("IVF_SEARCH_THREADS", "0") or 0)
        self.ivf_search_threads = threads if threads > 0 else default_ivf_search_threads()
        # the worker's batched index sync (spec 4.2, D8): one sync closes a batch of at
        # most SYNC_BATCH_JOBS jobs or SYNC_BATCH_MS ms, whichever comes first; 1 = per job
        self.sync_batch_jobs = int(os.environ.get("SYNC_BATCH_JOBS", "8"))
        self.sync_batch_ms = float(os.environ.get("SYNC_BATCH_MS", "1000"))
        if self.sync_batch_jobs < 1:
            raise ValueError(f"SYNC_BATCH_JOBS must be >= 1, not {self.sync_batch_jobs}")
        if not self.sync_batch_ms >= 0:  # also refuses nan
            raise ValueError(f"SYNC_BATCH_MS must be >= 0, not {self.sync_batch_ms}")
