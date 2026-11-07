"""Prepare the document-shaped Wikipedia corpus for bench/bench_docs.py.

The arxiv corpus has one chunk per document, so it cannot exercise document-scoped search,
summary-scope search or the per-document paths of ADRs 0007, 0010, 0013 and 0016. This corpus
has the shape of a document store (S.O.F.I.A.'s `legal_knowhow`: ~22k documents, one ~300-word
summary each plus 3-10 chunks of ~1000 tokens, ~165k records):

  one Wikipedia article = one document
    summary record  = the article's lead (the paragraphs before the first section heading),
                      cut to at most SUMMARY_WORDS words
    chunk records   = the rest of the article in ~CHUNK_WORDS-word windows on paragraph
                      boundaries, a section heading kept with the paragraph it introduces,
                      at most MAX_CHUNKS per document

Stage text : read the pinned parquet shards of legacy-datasets/wikipedia 20220301.en (the HF cache
             is reused when it holds them), select N_DOCS long articles in shard order, write
             bench/corpus-wiki/records.jsonl  {"id", "doc", "kind", "title", "text"} per record,
             grouped by document (summary first), and the query sets in queries.jsonl:
               summary  - an article's title; target: its summary record (search scope summaries)
               indoc    - one sentence of a chunk; target: that chunk, searched within its document
Stage embed: embed records.jsonl (as passages) and queries.jsonl (with Qwen3-Embedding's query
             instruction) against a local vLLM OpenAI endpoint into vecs.npy / qvecs.npy
             (float32, L2-normalised), crash-resumable via embed-progress.json

Run:  uv run --group bench --with pyarrow --with huggingface_hub python bench/prep_wiki.py --stage all
      (vLLM serving Qwen/Qwen3-Embedding-0.6B on --url, as for prep_arxiv.py)
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

REPO, CONFIG = "legacy-datasets/wikipedia", "20220301.en"
REVISION = "97a0b052c326b45fb68593a14972d9eed884cd17"
# in dump order (page id): the early shards hold the long, older articles; as many as needed are read
SHARDS = [f"train-{i:05d}-of-00041.parquet" for i in range(8)]
MODEL, DIMS = "Qwen/Qwen3-Embedding-0.6B", 1024
QUERY_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"

N_DOCS = 25_000
SUMMARY_WORDS, MIN_LEAD_WORDS = 300, 60
CHUNK_WORDS, MIN_TAIL_WORDS, MAX_CHUNKS = 800, 200, 10
MIN_BODY_WORDS = 2_400
N_SUMMARY_QUERIES, N_INDOC_QUERIES = 500, 500
INDOC_MIN_CHUNKS = 3
SENTENCE_WORDS = (8, 40)
SEED = 42

CORPUS = Path("bench/corpus-wiki")
RECORDS_PATH, QUERIES_PATH = CORPUS / "records.jsonl", CORPUS / "queries.jsonl"
META_PATH = CORPUS / "meta.json"
VEC_PATH, QVEC_PATH = CORPUS / "vecs.npy", CORPUS / "qvecs.npy"
PROGRESS_PATH = CORPUS / "embed-progress.json"

_END_PUNCT = tuple('.!?:;,"”)\']')


# ---------- article shaping (pure; tests/test_bench_docs.py) ----------

def is_heading(line: str) -> bool:
    """A section heading in the HF text dump: a short line of its own without closing
    punctuation ("Design", "Operational history"). List items look the same; they end
    the lead too, which is what a document's summary wants."""
    s = line.strip()
    return bool(s) and len(s.split()) <= 10 and not s.endswith(_END_PUNCT)


def split_article(text: str) -> tuple[list[str], list[str]]:
    """(lead paragraphs, body blocks). A body block is a paragraph, with the headings right
    above it prefixed, so a chunk never starts or ends on a bare heading."""
    lead, body, pending, in_body = [], [], [], False
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if is_heading(s):
            in_body = True
            pending.append(s)
            continue
        if in_body:
            body.append("\n".join(pending + [s]))
            pending = []
        else:
            lead.append(s)
    return lead, body


def words(s: str) -> int:
    return len(s.split())


def make_summary(lead: list[str], max_words: int = SUMMARY_WORDS) -> str:
    out = " ".join(lead).split()
    return " ".join(out[:max_words])


def make_chunks(body: list[str], target: int = CHUNK_WORDS, min_tail: int = MIN_TAIL_WORDS,
                max_chunks: int = MAX_CHUNKS) -> list[str]:
    """Greedy windows of whole blocks, each closed once it reaches `target` words; a last
    window under `min_tail` words joins the previous one; at most `max_chunks` are kept."""
    chunks, cur, n = [], [], 0
    for block in body:
        cur.append(block)
        n += words(block)
        if n >= target:
            chunks.append("\n\n".join(cur))
            cur, n = [], 0
    if cur:
        if chunks and n < min_tail:
            chunks[-1] += "\n\n" + "\n\n".join(cur)
        else:
            chunks.append("\n\n".join(cur))
    return chunks[:max_chunks]


def shape_article(page_id: str, title: str, text: str) -> list[dict] | None:
    """The records of one article, or None when it is not document-shaped (short lead,
    short body, a list page)."""
    if title.startswith("List of"):
        return None
    lead, body = split_article(text)
    if sum(map(words, lead)) < MIN_LEAD_WORDS or sum(map(words, body)) < MIN_BODY_WORDS:
        return None
    chunks = make_chunks(body)
    if len(chunks) < 2:
        return None
    doc = f"w{page_id}"
    recs = [{"id": doc, "doc": doc, "kind": "summary", "title": title, "text": make_summary(lead)}]
    recs += [{"id": f"{doc}#{i}", "doc": doc, "kind": "chunk", "title": title, "text": c}
             for i, c in enumerate(chunks)]
    return recs


_SENT = re.compile(r"(?<=[.!?])\s+")


def pick_sentence(text: str, rng: np.random.Generator) -> str | None:
    """One sentence of SENTENCE_WORDS words from a chunk (headings excluded), or None."""
    lo, hi = SENTENCE_WORDS
    cands = [s for para in text.split("\n") if not is_heading(para)
             for s in _SENT.split(para) if lo <= words(s) <= hi]
    return cands[int(rng.integers(len(cands)))] if cands else None


def build_queries(records: list[dict], seed: int = SEED, n_summary: int = N_SUMMARY_QUERIES,
                  n_indoc: int = N_INDOC_QUERIES) -> list[dict]:
    """The two query sets, drawn from the ingested documents (as a user asks about a
    document the store holds): summary queries are article titles aimed at the summary
    record; in-document queries are a sentence of a chunk aimed at that chunk within its
    document (documents with at least INDOC_MIN_CHUNKS chunks)."""
    rng = np.random.default_rng(seed)
    docs: dict[str, list[dict]] = {}
    for r in records:
        docs.setdefault(r["doc"], []).append(r)
    ids = sorted(docs)
    out = []
    for d in sorted(rng.choice(len(ids), size=min(n_summary, len(ids)), replace=False)):
        s = docs[ids[d]][0]
        out.append({"set": "summary", "text": s["title"], "doc": s["doc"], "target": s["id"]})
    eligible = [d for d in ids if sum(r["kind"] == "chunk" for r in docs[d]) >= INDOC_MIN_CHUNKS]
    n = 0
    for d in rng.permutation(len(eligible)):
        if n >= n_indoc:
            break
        chunks = [r for r in docs[eligible[d]] if r["kind"] == "chunk"]
        c = chunks[int(rng.integers(len(chunks)))]
        sent = pick_sentence(c["text"], rng)
        if sent:
            out.append({"set": "indoc", "text": sent, "doc": c["doc"], "target": c["id"]})
            n += 1
    return out


def query_input(text: str) -> str:
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery:{text}"


# ---------- stage: text ----------

def stage_text(args):
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq

    if PROGRESS_PATH.exists():
        sys.exit(f"{PROGRESS_PATH} exists: an embed run is checkpointed against the current "
                 "records; delete it and vecs.npy/qvecs.npy to regenerate the text")
    CORPUS.mkdir(parents=True, exist_ok=True)
    records, n_docs, used = [], 0, []
    for shard in SHARDS:
        if n_docs >= args.docs:
            break
        fp = hf_hub_download(REPO, f"data/{CONFIG}/{shard}", repo_type="dataset", revision=REVISION)
        used.append(shard)
        for batch in pq.ParquetFile(fp).iter_batches(batch_size=4096, columns=["id", "title", "text"]):
            cols = batch.to_pydict()
            for pid, title, text in zip(cols["id"], cols["title"], cols["text"]):
                recs = shape_article(pid, title, text)
                if recs:
                    records += recs
                    n_docs += 1
                    if n_docs >= args.docs:
                        break
            if n_docs >= args.docs:
                break
        print(f"{shard}: {n_docs:,} documents, {len(records):,} records so far", flush=True)
    if n_docs < args.docs:
        sys.exit(f"only {n_docs} document-shaped articles in {used}; add shards")
    queries = build_queries(records, seed=args.seed)
    with open(RECORDS_PATH, "wb") as f:
        for r in records:
            f.write(orjson.dumps(r) + b"\n")
    with open(QUERIES_PATH, "wb") as f:
        for q in queries:
            f.write(orjson.dumps(q) + b"\n")
    kinds = [r["kind"] for r in records]
    meta = {"model": MODEL, "dims": DIMS, "dataset": REPO, "config": CONFIG, "revision": REVISION,
            "shards": used, "seed": args.seed, "n_docs": n_docs, "n_records": len(records),
            "n_summaries": kinds.count("summary"), "n_chunks": kinds.count("chunk"),
            "n_queries": {s: sum(q["set"] == s for q in queries) for s in ("summary", "indoc")},
            "params": {"summary_words": SUMMARY_WORDS, "min_lead_words": MIN_LEAD_WORDS,
                       "chunk_words": CHUNK_WORDS, "min_tail_words": MIN_TAIL_WORDS,
                       "max_chunks": MAX_CHUNKS, "min_body_words": MIN_BODY_WORDS,
                       "sentence_words": list(SENTENCE_WORDS), "indoc_min_chunks": INDOC_MIN_CHUNKS,
                       "query_instruction": QUERY_INSTRUCTION},
            "words": {"summary_mean": float(np.mean([words(r["text"]) for r in records if r["kind"] == "summary"])),
                      "chunk_mean": float(np.mean([words(r["text"]) for r in records if r["kind"] == "chunk"]))}}
    META_PATH.write_text(json.dumps(meta, indent=1))
    print(json.dumps({k: meta[k] for k in ("n_docs", "n_records", "n_summaries", "n_chunks", "n_queries", "words")}),
          flush=True)


# ---------- stage: embed ----------

async def embed_batch(client, url, texts, stats, attempt=0):
    try:
        r = await client.post(url, json={"model": MODEL, "input": texts, "encoding_format": "base64",
                                         "truncate_prompt_tokens": 2048})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            if 400 <= code < 500 and code != 429:  # deterministic rejection: retrying won't help
                raise RuntimeError(f"server rejected batch ({code}): {e.response.text[:500]}") from e
        if attempt >= 3:
            raise
        stats["retries"] += 1
        await asyncio.sleep(2 ** attempt)
        return await embed_batch(client, url, texts, stats, attempt + 1)


def decode(resp, n):
    data = sorted(resp["data"], key=lambda d: d["index"])
    if [d["index"] for d in data] != list(range(n)):
        raise RuntimeError("response indices out of order")
    block = np.stack([np.frombuffer(base64.b64decode(d["embedding"]), dtype=np.float32) for d in data])
    if block.shape != (n, DIMS) or not np.isfinite(block).all():
        raise RuntimeError(f"bad embedding block {block.shape}")
    norms = np.linalg.norm(block, axis=1, keepdims=True)
    if not norms.all():
        raise RuntimeError("zero-norm embedding")
    return block / norms


async def embed_file(texts_iter, n, path, args, label, progress_key):
    """Embed n texts into the float32 .npy at path, checkpointing a contiguous prefix."""
    prog = json.loads(PROGRESS_PATH.read_text()) if PROGRESS_PATH.exists() else {}
    if path.exists():
        vecs = np.lib.format.open_memmap(path, mode="r+")
        if vecs.shape != (n, DIMS):
            sys.exit(f"{path} is {vecs.shape}, expected ({n}, {DIMS}); delete it and {PROGRESS_PATH}")
    else:
        if prog.get(progress_key):
            sys.exit(f"{path} is missing but {PROGRESS_PATH} has progress for it")
        vecs = np.lib.format.open_memmap(path, mode="w+", dtype=np.float32, shape=(n, DIMS))
    start = prog.get(progress_key, 0)
    texts = list(texts_iter)
    stats, t0 = {"retries": 0, "tokens": 0}, time.monotonic()
    sem = asyncio.Semaphore(args.concurrency)

    async def one(client, row, batch):
        async with sem:
            resp = await embed_batch(client, f"{args.url}/embeddings", batch, stats)
        vecs[row:row + len(batch)] = decode(resp, len(batch))
        stats["tokens"] += (resp.get("usage") or {}).get("prompt_tokens", 0)

    window = args.batch * args.concurrency * 4
    async with httpx.AsyncClient(timeout=300) as client:
        row = start
        while row < n:
            end = min(row + window, n)
            await asyncio.gather(*[one(client, i, texts[i:min(i + args.batch, end)])
                                   for i in range(row, end, args.batch)])
            row = end
            vecs.flush()
            prog[progress_key] = row
            tmp = PROGRESS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(prog))
            os.replace(tmp, PROGRESS_PATH)
            el = time.monotonic() - t0
            print(f"[{label}] {row:,}/{n:,} | {(row - start) / el:,.0f} rows/s | "
                  f"{stats['tokens'] / el:,.0f} tok/s | {el / 60:.1f}m | retries {stats['retries']}", flush=True)
    bad = int((~vecs.any(axis=1)).sum())
    if bad:
        sys.exit(f"{path}: {bad} zero rows after embedding")
    print(f"[{label}] done: {n:,} rows, {stats['tokens']:,} tokens, {(time.monotonic() - t0) / 60:.1f}m", flush=True)


def stage_embed(args):
    meta = json.loads(META_PATH.read_text())
    if (meta["model"], meta["dims"]) != (MODEL, DIMS):
        sys.exit(f"meta says {meta['model']}/{meta['dims']}d, this script embeds {MODEL}/{DIMS}d")

    def lines(path, key):
        with open(path, "rb") as f:
            for line in f:
                yield key(orjson.loads(line))

    asyncio.run(embed_file(lines(RECORDS_PATH, lambda r: r["text"]), meta["n_records"], VEC_PATH,
                           args, "records", "records"))
    nq = sum(meta["n_queries"].values())
    asyncio.run(embed_file(lines(QUERIES_PATH, lambda q: query_input(q["text"])), nq, QVEC_PATH,
                           args, "queries", "queries"))
    meta["embedded"] = True
    META_PATH.write_text(json.dumps(meta, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["text", "embed", "all"])
    ap.add_argument("--url", default="http://localhost:8001/v1")
    ap.add_argument("--docs", type=int, default=N_DOCS)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    if args.stage in ("text", "all"):
        stage_text(args)
    if args.stage in ("embed", "all"):
        stage_embed(args)


if __name__ == "__main__":
    main()
