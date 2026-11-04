import asyncio
import gc
import hmac
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Settings
from .store import (
    DATA_DIR_LOCK_POLL_S,
    CollectionDeletedError,
    CollectionManager,
    _log,
    _normalize,
    hash_key,
    lock_data_dir,
    open_data_dir_lock,
)


def _gil_enabled() -> bool:
    """False only on a free-threaded build (3.13t+) that is really running without the GIL."""
    return getattr(sys, "_is_gil_enabled", lambda: True)()


# ---- request models ----

class RecordIn(BaseModel):
    text: str | None = None
    vector: list[float] | None = None
    metadata: dict[str, Any] | None = None


class ChunkIn(RecordIn):
    id: str
    position: int | None = None


class DocumentIn(BaseModel):
    doc_id: str
    summary: RecordIn | None = None
    chunks: list[ChunkIn] = []


class IngestIn(BaseModel):
    documents: list[DocumentIn] = Field(min_length=1)


class CollectionIn(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    dim: int | None = None
    bit_width: Literal[2, 4] = 4
    model: str | None = None
    base_url: str | None = None
    collection_key: str | None = None
    tokenizer: Literal["unicode61", "trigram"] = "unicode61"


class QueryIn(BaseModel):
    text: str | None = None
    vector: list[float] | None = None


class ExpandIn(BaseModel):
    siblings_topk: int | None = Field(default=None, ge=1)
    siblings_all: bool = False
    summary: bool = False


class SearchIn(BaseModel):
    query: QueryIn
    mode: Literal["vector", "text", "hybrid"] = "vector"
    k: int = Field(default=10, ge=1, le=1000)
    scope: Literal["chunks", "summaries", "both"] = "chunks"
    filter: dict[str, Any] | None = None
    expand: ExpandIn | None = None
    # IVF speed/recall knob: shards probed for this query (ignored without an index)
    nprobe: int | None = Field(default=None, ge=1, le=4096)
    # hybrid only: how the two legs are fused. alpha weighs the vector leg in relative
    # fusion (0 = BM25 only, 1 = vector only); rrf ignores it
    fusion: Literal["rrf", "relative"] = "rrf"
    alpha: float = Field(default=0.5, ge=0, le=1)


class IndexIn(BaseModel):
    nlist: int | None = Field(default=None, ge=2, le=4096)  # default: ~rows/8192, power of 2
    nprobe: int | None = Field(default=None, ge=1, le=4096)


class PatchIn(BaseModel):
    metadata: dict[str, Any]  # RFC 7396 merge patch: null removes a key
    apply_to_chunks: bool = True


def create_app(settings: Settings | None = None, embedder_factory=None) -> FastAPI:
    settings = settings or Settings()
    if not settings.root_api_key:
        raise RuntimeError("ROOT_API_KEY must be set")
    # opened by start() once this process holds the data-dir lock (ADR 0007): every
    # request but /healthz answers 503 until then (require_ready)
    manager: CollectionManager | None = None
    status = "starting"  # then "ok", or "failed" if start() raised
    housekeeping: asyncio.Task | None = None

    async def start(lock_fd: int | None, locked: bool) -> None:
        """Wait for the data-dir lock if another process holds it, then open the catalog,
        replay unfinished jobs and start housekeeping."""
        nonlocal manager, status, housekeeping
        try:
            if not locked:
                _log.info("waiting for data-dir lock %s", Path(settings.data_dir) / "raggio.lock")
                t0 = time.monotonic()
                while not await asyncio.to_thread(lock_data_dir, lock_fd):
                    await asyncio.sleep(DATA_DIR_LOCK_POLL_S)
                _log.info("acquired data-dir lock after %.1f s", time.monotonic() - t0)
            manager = CollectionManager(settings, embedder_factory)
            await manager.resume_pending()
            if settings.gc_freeze:
                # after resume_pending: what it loaded is startup heap too. A frozen
                # collection that is evicted or deleted is still freed, by reference counting
                gc.collect()
                gc.freeze()
            housekeeping = asyncio.create_task(manager.housekeeping())
            status = "ok"
        except Exception:
            status = "failed"
            _log.exception("start-up failed")
            raise

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lock_fd = open_data_dir_lock(settings.data_dir)
        locked = lock_fd is None or await asyncio.to_thread(lock_data_dir, lock_fd)
        startup = asyncio.create_task(start(lock_fd, locked))
        if locked:
            # the usual start: start-up finishes before the server answers, as it always
            # has. While another process holds the lock (the old replica of a rolling
            # restart) it goes on in the background: the platform stops the old replica
            # only once this one is up and answering /healthz
            await startup
        yield
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        if housekeeping:
            housekeeping.cancel()
        if manager:
            await manager.shutdown()
        if lock_fd is not None:
            os.close(lock_fd)  # releases the lock: every database of DATA_DIR is closed
        if settings.gc_freeze:
            gc.unfreeze()

    async def require_ready(request: Request) -> None:
        if status != "ok" and request.scope["path"] != "/healthz":
            detail = "waiting for the data directory" if status == "starting" else "start-up failed"
            raise HTTPException(503, detail, headers={"Retry-After": "2"})

    app = FastAPI(title="raggio", lifespan=lifespan, dependencies=[Depends(require_ready)])

    # ---- auth ----

    def request_key(request: Request) -> str:
        key = request.headers.get("x-api-key", "")
        if not key:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                key = auth[7:]
        return key

    def is_root(key: str) -> bool:
        return hmac.compare_digest(key, settings.root_api_key)

    def require_root(request: Request) -> None:
        if not is_root(request_key(request)):
            raise HTTPException(401, "root API key required")

    def require_collection(name: str, request: Request) -> str:
        key = request_key(request)
        if is_root(key):
            return name
        cfg = manager.get_config(name)
        if cfg and cfg.key_hash and hmac.compare_digest(hash_key(key), cfg.key_hash):
            return name
        raise HTTPException(401, "invalid API key for this collection")

    async def get_collection(name: str = Depends(require_collection)):
        try:
            return await manager.touch(name)
        except KeyError:
            raise HTTPException(404, f"collection '{name}' not found")

    # ---- collections ----

    @app.post("/collections", status_code=201, dependencies=[Depends(require_root)])
    async def create_collection(body: CollectionIn):
        try:
            cfg = await manager.create_collection(
                body.name, body.dim, body.bit_width, body.model, body.base_url,
                body.collection_key, body.tokenizer,
            )
        except ValueError as e:
            raise HTTPException(409 if "already exists" in str(e) else 400, str(e))
        return {"name": cfg.name, "dim": cfg.dim, "bit_width": cfg.bit_width,
                "tokenizer": cfg.tokenizer}

    @app.get("/collections", dependencies=[Depends(require_root)])
    async def list_collections():
        return {"collections": manager.list_collections()}

    @app.get("/collections/{name}")
    async def collection_info(c=Depends(get_collection)):
        # off the loop: the GROUP BY and COUNT(DISTINCT doc_id) walk every record
        # (the covering index since plan C: about 96 MB read cold at 2.55M rows, p1)
        stats = await asyncio.to_thread(c.stats)
        return {"name": c.cfg.name, "dim": c.cfg.dim, "bit_width": c.cfg.bit_width,
                "model": c.cfg.model, "tokenizer": c.cfg.tokenizer,
                "index": c.index_info(), **stats}

    # ---- optional IVF index: an additional object, added/removed per collection ----

    @app.post("/collections/{name}/index", status_code=202)
    async def create_index(body: IndexIn | None = None, c=Depends(get_collection)):
        """(Re)build an IVF index over the collection; nprobe-only body on an indexed
        collection just retunes the default. Runs as a job — poll the returned job_id."""
        body = body or IndexIn()
        try:
            job_id = await c.request_index(body.nlist, body.nprobe)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"job_id": job_id}

    @app.delete("/collections/{name}/index", status_code=202)
    async def delete_index(c=Depends(get_collection)):
        try:
            job_id = await c.request_index_drop()
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"job_id": job_id}

    @app.delete("/collections/{name}", dependencies=[Depends(require_root)])
    async def delete_collection(name: str):
        if manager.get_config(name) is None:
            raise HTTPException(404, f"collection '{name}' not found")
        await manager.delete_collection(name)
        return {"deleted": name}

    # ---- ingest / documents ----

    def _check_record(r: RecordIn, dim: int, label: str) -> None:
        if r.text is None and r.vector is None:
            raise HTTPException(400, f"{label}: needs text and/or vector")
        if r.vector is not None and len(r.vector) != dim:
            raise HTTPException(400, f"{label}: vector has dim {len(r.vector)}, collection expects {dim}")

    @app.post("/collections/{name}/documents", status_code=202)
    async def ingest(body: IngestIn, c=Depends(get_collection)):
        for d in body.documents:
            if d.summary:
                _check_record(d.summary, c.cfg.dim, f"document '{d.doc_id}' summary")
            for ch in d.chunks:
                _check_record(ch, c.cfg.dim, f"chunk '{ch.id}'")
        job_id = await c.enqueue(body.model_dump())
        return {"job_id": job_id}

    @app.get("/collections/{name}/documents/{doc_id}")
    async def get_document(doc_id: str, c=Depends(get_collection)):
        doc = c.get_document(doc_id)  # sync, on the event loop thread: its reads are guarded
        if doc is None:
            raise HTTPException(404, f"document '{doc_id}' not found")
        return doc

    @app.get("/collections/{name}/documents")
    async def list_documents(
        c=Depends(get_collection),
        scope: Literal["chunks", "summaries", "both"] = "both",
        filter: str | None = Query(default=None, description="JSON object; same grammar as search"),
        sort: str | None = Query(default=None, description="metadata key; '-' prefix = descending"),
        limit: int = Query(default=20, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        include_vector: bool = False,
    ):
        """Unranked listing with total count: the no-query counterpart of search."""
        filt = None
        if filter:
            try:
                filt = json.loads(filter)
            except ValueError:
                filt = filter  # not a dict -> rejected below
            if not isinstance(filt, dict):
                raise HTTPException(400, "filter must be a JSON object")
        try:
            return await asyncio.to_thread(c.list_records, scope, filt, sort, limit, offset, include_vector)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.patch("/collections/{name}/documents/{doc_id}")
    async def patch_document(doc_id: str, body: PatchIn, c=Depends(get_collection)):
        """Merge-patch metadata on the document's records without re-ingesting."""
        n = await c.patch_metadata(doc_id, body.metadata, body.apply_to_chunks)
        if not n:
            raise HTTPException(404, f"document '{doc_id}' not found")
        return {"patched_records": n}

    @app.delete("/collections/{name}/documents/{doc_id}")
    async def delete_document(doc_id: str, c=Depends(get_collection)):
        deleted = await c.delete_document(doc_id)
        if not deleted:
            raise HTTPException(404, f"document '{doc_id}' not found")
        return {"deleted_records": deleted}

    @app.get("/collections/{name}/jobs/{job_id}")
    async def job_status(job_id: int, c=Depends(get_collection)):
        # runs on the event loop thread (a short point read), so it uses the loop
        # thread's read connection, through the same guard as every unlocked read
        with c._reading() as db:
            row = db.execute(
                "SELECT status, error, created_at, updated_at FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(404, f"job {job_id} not found")
        return {"job_id": job_id, "status": row[0], "error": row[1],
                "created_at": row[2], "updated_at": row[3]}

    # ---- search ----

    @app.post("/collections/{name}/search")
    async def search(body: SearchIn, c=Depends(get_collection)):
        q = body.query
        if q.text is not None and not q.text.strip():
            raise HTTPException(400, "query text must be non-empty")
        if body.mode == "vector":
            if (q.text is None) == (q.vector is None):
                raise HTTPException(400, "vector mode needs exactly one of text or vector")
        elif q.text is None:  # text and hybrid run BM25, which needs the raw text
            raise HTTPException(400, f"{body.mode} mode needs a text query")
        if body.mode == "text" and q.vector is not None:
            raise HTTPException(400, "text mode does not accept a vector")
        if q.vector is not None and len(q.vector) != c.cfg.dim:
            raise HTTPException(400, f"query vector has dim {len(q.vector)}, collection expects {c.cfg.dim}")
        try:
            qvec = None
            if body.mode != "text":
                # in hybrid mode a supplied vector skips the embedding call
                vec = q.vector
                if vec is None:
                    try:
                        vec = (await c.embedder.embed([q.text]))[0]
                    except Exception:
                        if c.deleted:  # DELETE's stop() closed the embedder under this call
                            raise CollectionDeletedError(c.cfg.name) from None
                        raise
                qvec = _normalize(np.array([vec], dtype=np.float32))
            hits = await c.search(body.mode, qvec, q.text, body.k, body.scope, body.filter,
                                  body.expand, body.nprobe, body.fusion, body.alpha)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except CollectionDeletedError:  # DELETE /collections/{name} landed first
            raise HTTPException(404, f"collection '{c.cfg.name}' not found")
        return {"hits": hits}

    @app.get("/healthz")
    async def healthz():
        if status != "ok":
            # 200 while waiting for the data-dir lock, so a rolling restart goes on to stop
            # the old process; 503 once start-up failed, so the platform restarts this one
            return JSONResponse({"status": status}, status_code=200 if status == "starting" else 503)
        return {
            "status": "ok",
            "resident_collections": list(manager.resident),
            "bm25": manager.bm25_backend(),
            "gil_enabled": _gil_enabled(),
        }

    return app
