import os


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
