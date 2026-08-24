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
