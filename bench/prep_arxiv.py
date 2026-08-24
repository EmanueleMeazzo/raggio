"""Prepare the arxiv-abstracts corpus for bench.py.

Stage text : download HF dataset Rendra8631/arxiv-papers (parquet), filter/dedupe, write
             bench/corpus/abstracts.jsonl   line i <-> vector row i: {"title", "text"}
             bench/corpus/embed-meta.json   {"model", "dims", "paths": [...]}
Stage embed: embed abstracts.jsonl against a local vLLM OpenAI endpoint into
             bench/corpus/embed-vecs.npy    float32 N x DIM, crash-resumable via
             bench/corpus/prep-progress.json (contiguous-prefix checkpoint)

Run:  uv run --with pyarrow --with huggingface_hub python bench/prep_arxiv.py --stage all
Smoke/calibrate: --stage embed --limit 20000  (prints rows/s + extrapolated full ETA;
                 a later full run resumes from the checkpoint, nothing is re-embedded)
"""
import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import orjson

REPO = "Rendra8631/arxiv-papers"
MODEL = "Qwen/Qwen3-Embedding-0.6B"
DIMS = 1024
CORPUS = Path("bench/corpus")
TEXTS_PATH = CORPUS / "abstracts.jsonl"
META_PATH = CORPUS / "embed-meta.json"
VEC_PATH = CORPUS / "embed-vecs.npy"
PROGRESS_PATH = CORPUS / "prep-progress.json"


# ---------- stage: text ----------

def derive_year(submission_date, arxiv_id):
    m = re.search(r"\b(19|20)\d{2}\b", submission_date or "")
    if m:
        return m.group(0)
    # new-style "2301.12345" or old-style "hep-th/9901001": YYMM prefix, arxiv started 1991
    m = re.match(r"(\d{2})\d{2}\.", arxiv_id) or re.search(r"/(\d{2})\d{2}", arxiv_id)
    if m:
        yy = int(m.group(1))
        return str(1900 + yy if yy >= 91 else 2000 + yy)
    return "na"


def stage_text():
    from huggingface_hub import snapshot_download
    import pyarrow.parquet as pq

    if PROGRESS_PATH.exists():
        sys.exit(f"{PROGRESS_PATH} exists — an embed run is checkpointed against the current "
                 "abstracts.jsonl and rewriting it would silently misalign rows. Resume with "
                 "--stage embed, or delete prep-progress.json and embed-vecs.npy to regenerate.")
    CORPUS.mkdir(parents=True, exist_ok=True)
    print(f"downloading {REPO} parquet files...", flush=True)
    root = snapshot_download(repo_id=REPO, repo_type="dataset", allow_patterns=["*.parquet"])
    files = sorted(Path(root).rglob("*.parquet"))  # sorted order = row-alignment invariant
    if not files:
        sys.exit(f"no parquet files under {root}")

    paths, seen = [], set()
    skipped_empty = skipped_dup = 0
    with open(TEXTS_PATH, "wb") as out:
        for fi, fp in enumerate(files, 1):
            kept0 = len(paths)
            f_in = f_empty = f_dup = 0
            for batch in pq.ParquetFile(fp).iter_batches(
                    batch_size=8192, columns=["arxiv_id", "title", "abstract", "submission_date"]):
                cols = batch.to_pydict()
                for aid, title, abstract, sdate in zip(
                        cols["arxiv_id"], cols["title"], cols["abstract"], cols["submission_date"]):
                    f_in += 1
                    aid = (aid or "").strip()
                    abstract = (abstract or "").strip()
                    if not aid or not abstract:
                        f_empty += 1
                        continue
                    if aid in seen:  # duplicate doc_ids would upsert and break the count assert
                        f_dup += 1
                        continue
                    seen.add(aid)
                    title = (title or "").strip() or abstract[:60]  # hybrid queries need non-empty titles
                    out.write(orjson.dumps({"title": title, "text": f"{title}\n\n{abstract}"}) + b"\n")
                    paths.append(f"arxiv/{derive_year(sdate, aid)}/{aid.replace('/', '-')}")
            skipped_empty += f_empty
            skipped_dup += f_dup
            print(f"file {fi}/{len(files)} {fp.name}: {f_in} rows in, {len(paths) - kept0} kept, "
                  f"{f_empty} empty, {f_dup} dup-id", flush=True)

    if len(paths) != len(set(paths)):
        sys.exit("path collision after '/'->'-' rewrite — investigate before embedding")
    tmp = META_PATH.with_suffix(".json.tmp")
    tmp.write_bytes(orjson.dumps({"model": MODEL, "dims": DIMS, "n": len(paths),
                                  "texts_file": TEXTS_PATH.name,  # binds abstracts.jsonl to this corpus
                                  "dataset_revision": Path(root).name,  # hf snapshot dir = commit sha
                                  "paths": paths}))
    os.replace(tmp, META_PATH)
    print(f"text stage done: {len(paths)} rows kept ({skipped_empty} empty, {skipped_dup} dup-id "
          f"skipped) -> {TEXTS_PATH}, {META_PATH}", flush=True)
    return len(paths)


# ---------- stage: embed ----------

def open_vecs(n):
    if VEC_PATH.exists():
        mm = np.lib.format.open_memmap(VEC_PATH, mode="r+")
        if mm.shape != (n, DIMS) or mm.dtype != np.float32:
            sys.exit(f"{VEC_PATH} is {mm.shape} {mm.dtype}, expected ({n}, {DIMS}) float32 — "
                     "delete it (and prep-progress.json) to restart")
        return mm
    if PROGRESS_PATH.exists():
        sys.exit(f"{VEC_PATH} is missing but {PROGRESS_PATH} exists — resuming would leave the "
                 "checkpointed rows as zeros; delete prep-progress.json to start fresh")
    return np.lib.format.open_memmap(VEC_PATH, mode="w+", dtype=np.float32, shape=(n, DIMS))


def verify(vecs, upto):
    bad = 0
    for s in range(0, upto, 100_000):
        block = vecs[s:min(s + 100_000, upto)]  # never scan past upto — later rows are legitimately zero
        bad += int((~block.any(axis=1)).sum()) + int((~np.isfinite(block).all(axis=1)).sum())
    print(f"verification: {bad} zero/non-finite rows in first {upto:,} (expected 0)", flush=True)
    if bad:
        sys.exit(1)


async def embed_batch(client, url, texts, stats, attempt=0):
    try:
        r = await client.post(url, json={"model": MODEL, "input": texts,
                                         "encoding_format": "base64",
                                         "truncate_prompt_tokens": 2048})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            if 400 <= code < 500 and code != 429:  # deterministic rejection — retrying won't help
                raise RuntimeError(f"server rejected batch ({code}): {e.response.text[:500]}") from e
        if attempt >= 3:
            raise
        stats["retries"] += 1
        await asyncio.sleep(2 ** attempt)
        return await embed_batch(client, url, texts, stats, attempt + 1)


async def stage_embed(args):
    meta = json.loads(META_PATH.read_text())
    if meta.get("model") != MODEL or meta.get("dims") != DIMS:
        sys.exit(f"meta says {meta.get('model')}/{meta.get('dims')}d but this script embeds "
                 f"{MODEL}/{DIMS}d — re-run --stage text after changing the constants")
    n = len(meta["paths"])
    with open(TEXTS_PATH, "rb") as f:
        lines = sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 24), b""))
    if lines != n:
        sys.exit(f"{TEXTS_PATH} has {lines} lines but meta lists {n} paths — corpus files are "
                 "out of sync; re-run --stage text")
    limit = min(args.limit or n, n)
    vecs = open_vecs(n)
    jsonl_size = TEXTS_PATH.stat().st_size
    start_row = 0
    if PROGRESS_PATH.exists():
        prog = json.loads(PROGRESS_PATH.read_text())
        if prog.get("jsonl_size") != jsonl_size:  # checkpoint taken against a different corpus
            sys.exit(f"checkpoint jsonl_size {prog.get('jsonl_size')} != current {jsonl_size} — "
                     "abstracts.jsonl changed under the checkpoint; delete prep-progress.json "
                     "and embed-vecs.npy to restart")
        start_row = prog["next_row"]
    if start_row >= limit:
        print(f"nothing to embed: checkpoint at row {start_row}, limit {limit}", flush=True)
        verify(vecs, limit)
        return
    print(f"embedding rows {start_row}..{limit} of {n} ({MODEL}, {DIMS}d, "
          f"batch {args.batch} x concurrency {args.concurrency}, {args.url})", flush=True)

    stats = {"rows": start_row, "tokens": 0, "retries": 0}
    t0 = time.monotonic()

    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            done, el = stats["rows"], time.monotonic() - t0
            rps = (done - start_row) / el if el > 0 else 0
            eta = (limit - done) / rps if rps > 0 else float("inf")
            print(f"[embed] {done:,}/{limit:,} ({done / limit:.1%}) | {rps:,.0f} rows/s | "
                  f"{stats['tokens'] / el:,.0f} tok/s | elapsed {el / 60:.0f}m | "
                  f"ETA {eta / 60:.0f}m | retries {stats['retries']}", flush=True)

    sem = asyncio.Semaphore(args.concurrency)  # gather queues a whole window; this bounds in-flight

    async def one(client, row, texts):
        async with sem:
            resp = await embed_batch(client, f"{args.url}/embeddings", texts, stats)
        data = sorted(resp["data"], key=lambda d: d["index"])
        if [d["index"] for d in data] != list(range(len(texts))):
            raise RuntimeError(f"rows {row}..{row + len(texts)}: response indices != 0..{len(texts) - 1}")
        # each embedding is its own base64 string (padded) — decode individually, never concat
        block = np.stack([np.frombuffer(base64.b64decode(d["embedding"]), dtype=np.float32)
                          for d in data])
        if block.shape != (len(texts), DIMS):
            raise RuntimeError(f"rows {row}..{row + len(texts)}: shape {block.shape} != ({len(texts)}, {DIMS})")
        if not np.isfinite(block).all():
            raise RuntimeError(f"rows {row}..{row + len(texts)}: non-finite embedding from server")
        norms = np.linalg.norm(block, axis=1, keepdims=True)
        if not norms.all():
            raise RuntimeError(f"rows {row}..{row + len(texts)}: zero-norm embedding from server")
        vecs[row:row + len(texts)] = block / norms
        stats["rows"] += len(texts)  # per-batch, so the 30s heartbeat moves inside a window
        stats["tokens"] += (resp.get("usage") or {}).get("prompt_tokens", 0)

    hb = asyncio.create_task(heartbeat())
    window = args.batch * args.concurrency * 4  # rows gathered per checkpoint
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            with open(TEXTS_PATH, "rb") as f:
                for _ in range(start_row):
                    next(f)
                row = start_row
                while row < limit:
                    w_texts = [orjson.loads(next(f))["text"] for _ in range(min(window, limit - row))]
                    await asyncio.gather(*[
                        one(client, row + i, w_texts[i:i + args.batch])
                        for i in range(0, len(w_texts), args.batch)])
                    row += len(w_texts)
                    stats["rows"] = row
                    vecs.flush()
                    tmp = PROGRESS_PATH.with_suffix(".tmp")  # atomic: a crash mid-write must not brick resume
                    tmp.write_text(json.dumps({"next_row": row, "jsonl_size": jsonl_size}))
                    os.replace(tmp, PROGRESS_PATH)
    finally:
        hb.cancel()

    el = time.monotonic() - t0
    rps = (limit - start_row) / el
    print(f"embed done: rows {start_row}..{limit} in {el / 60:.1f}m | {rps:,.0f} rows/s | "
          f"{stats['tokens']:,} tokens | retries {stats['retries']} | "
          f"full-corpus extrapolation: {n / rps / 3600:.1f}h", flush=True)
    verify(vecs, limit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["text", "embed", "all"])
    ap.add_argument("--url", default="http://localhost:8001/v1")
    ap.add_argument("--batch", type=int, default=256, help="texts per /v1/embeddings request")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="embed only the first N rows (0 = all)")
    args = ap.parse_args()
    if args.stage in ("text", "all"):
        stage_text()
    if args.stage in ("embed", "all"):
        asyncio.run(stage_embed(args))


if __name__ == "__main__":
    main()
