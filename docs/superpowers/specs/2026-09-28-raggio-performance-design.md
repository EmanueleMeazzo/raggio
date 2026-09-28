# raggio performance program (items 1–4) — design & cross-plan contract

Status: draft for review · Date: 2026-09-28 · Base: `main` @ 19a8a2e

This is the spec that the seven implementation plans in `docs/superpowers/plans/2026-09-28-*.md`
argue from. It records the decisions, the ownership of shared code, and the benchmark
protocol. The evidence behind every claim is in `docs/superpowers/research/2026-09-28-*.md`:
seven research briefs plus an adversarial critic. Where a brief and the critic disagree,
**this document wins**. The "Evidence" column in §3 names the brief.

## 1. Context and goals

The C-port analysis concluded that raggio should stay in Python and take four targeted steps.
The reference is the **2026-09-28 DGX baseline of main @19a8a2e**
(`docs/superpowers/research/2026-09-28-dgx-baseline.md`): DGX Spark, 2,549,119 × 1024 vectors,
a 4 GiB cap and 500 held-out queries. It supersedes the older published
`bench/results-arxiv.md` numbers, which predate later commits. The main numbers are:

- **Search latency (flat):** vector p50 20.7–21.3 ms, filtered p50 25–28 ms.
- **Hybrid:** p50 86–89 ms, p95 ~161 ms, p99 ~557 ms, QPS concurrent 13.8.
- **Throughput:** flat QPS concurrent 117. IVF QPS concurrent 166–214 (noisy).
- **IVF:** serial p50 11.1 ms, filtered p50 44–70 ms (noisy), build at 4 GiB 213 s.
  - Most of the IVF noise comes from numpy's OpenBLAS: 20 busy-spinning threads burn about 16 cores under any search load and push query threads onto the slow A725 cores (p3, D16).
  - The filtered cost is `_IvfIndex._intersect`: 29.8 of 31.9 ms in-process (p3).
- **Ingest:** 1879 s (1357 vec/s) — published, not re-measured.
- **Cold start (container start → first `GET /collections/bench`):**
  - 120–125 s true-cold (fadvise-evicted, probe p1) and 128 s after the reboot, with the event loop blocked the whole time;
  - ~31 s partly warm;
  - 4.6–5.4 s warm.
- **Every later `GET /collections/{name}` under 4 GiB:** about 41 s, with the loop blocked. `stats()` re-scans the 3.57 GB records table, which the cgroup page cache cannot hold (p1).
- **Hybrid tail:** p99 ~555 ms. The cause is 24 underscore titles (`_prune_common`'s `\w+`), not stage 2 (p2).

The probe evidence is in `research/2026-09-28-dgx-probe-p{1,2,…}-*.md` and summarized in §8.

| Item | Goal | Plan(s) |
|---|---|---|
| 1 | Replace the pure-Python BM25 stage-2 rescorer and tokenizer with a first-party Rust extension. Keep a Python fallback and change no rankings. | E |
| 2 | Remove Python-side inefficiencies: correctness races, cold start and event-loop blocking, the ingest path. | B, C, D (A is the shared foundation) |
| 3 | Parallel IVF shard fan-out raggio-side, plus local drafts of the upstream turbovec asks (ANN/partitioned search, cp314t wheels). | F |
| 4 | Evaluate free-threaded CPython 3.14t on an experimental image. | G |

Non-goals: rewriting raggio in C or Rust, forking turbovec, changing ranking/recall
semantics (except where a plan explicitly measures and gates it), posting anything
upstream (the maintainer posts personally).

## 2. Plan set and execution order

| Order | Plan file | Scope |
|---|---|---|
| 0 | `…-bench-and-packaging-foundation.md` (A), bench tasks only | Harden `bench/bench.py`. Commit the DGX runbook scripts. The baseline of 19a8a2e was already captured on 2026-09-28 with a scratch-patched bench (`research/2026-09-28-dgx-baseline.md`). A's DGX step is an **equivalence re-baseline**: the hardened bench on the same image must land inside the baseline's noise band. |
| 1 | B `…-concurrency-correctness.md` | R1 catalog lock, R3 stop() under lock, R4 indexed_counts copy-on-write, delete_collection vs `_load_lock`, concurrency stress test. |
| 2 | A (rest) | uv ≥ 0.12 + `required-version`, re-lock, orjson → `bench` group, minimal CI, Dockerfile base migration (see §4.2) plus `ENV OPENBLAS_NUM_THREADS=1` (D16). The DGX step records a **new reference baseline** for the PBS interpreter, SQLite 3.50.4 and single-threaded BLAS (D15, D16). The IVF build time and the flat vector rows must not regress beyond the band. Hybrid rows and IVF concurrent/filtered rows are expected to move and are reported. |
| 3 | C `…-cold-start-and-loop-hygiene.md` | Covering + partial indexes with the sorted-listing plan pin (D9), numpy ghost diff, `Collection(...)` built off the loop, `stats()` off the loop and cheap, `_vec_sample` rowid sampling with a sparse-id fallback, memory release (D13: trim threshold ENV + `malloc_trim(0)`), headroom guard before pre-work with the measured need, `_backfill_vecs` without the full `records` scan, attach-replay cost. |
| 4 | D `…-ingest-path.md`, phase D1 | Payload encode/decode off the loop, upsert ordering and rollback, `indexed_counts` after commit, binary job journal (reads legacy JSON), vacuum policy. |
| 5 | E `…-native-bm25.md` | First, the prune-tokenizer fix (D14) as its own task and DGX arm. Then the `raggio-native` maturin crate, parity goldens, fallback, `NATIVE_BM25` knob, Rust builder stage in the Dockerfile, and ADR 0004. |
| 6 | F `…-ivf-fanout-and-upstream.md` | `_IvfIndex` search fan-out: plain per-(query, shard) nq=1, default 12 threads, no audience batching (D3). Shard-side allowlist intersection, done **inside each pool task**. R2/R2b generation counters, dirty-shard sync, the 32,768-row pooled-path cliff documented and reported, upstream drafts (local), ADR 0005. |
| 7 | D, phase D2 | Batched worker and batched index sync with done-after-sync semantics and a flush in `stop()`. Note the change at ADR 0001:50. |
| 8 | G `…-free-threaded-python.md` | `PYTHON=3.14t` build arg on the unified Dockerfile, a turbovec sdist build in the builder stage, GIL guard, `GC_FREEZE` knob, and an ABAB A/B on the DGX. Experimental image only; ADR 0006. |

Every plan ships as its own branch/PR. It must leave `uv run pytest -q` green and must not
depend on a later plan. Before each plan's DGX A/B, the preceding `main` is re-baselined;
A/Bs compare against that, never against the step-0 numbers.

## 3. Decisions (adopted defaults — the user may override at plan review)

| # | Decision | Default adopted | Evidence |
|---|---|---|---|
| D1 | ADR numbering | 0004 native BM25 (E). 0005 IVF fan-out plus the upstream stance (F), partly superseding ADR 0001:48 for items 1 and 3; E's ADR 0004 also cites the 0001:48 supersession for first-party native code. 0006 free-threading (G). B, C and D each append their own `## Addendum 2026-09 — <topic>` section to ADR 0001. C also corrects the stale 0001:33 row, and D updates the 0001:50 lever note. | critic |
| D2 | Dockerfile | One parameterized multi-stage Dockerfile. `ARG PYTHON=3.12` and `ARG UV_VERSION` pinned. uv binary from `ghcr.io/astral-sh/uv:<UV_VERSION>`. `uv python install` into `/python`. Runtime `debian:trixie-slim`. The bookworm uv tags are frozen at 0.9.30. No separate `Dockerfile.ft`. A migrates the base, E adds the Rust builder stage, G adds the 3.14t/turbovec-sdist path. | 4a, critic |
| D3 | Fan-out grouping | **Settled by p3: plain per-(query, shard) nq=1 fan-out on a thread pool (3a). Audience batching (3b) is not shipped.** Grouping never beat q1 beyond noise on aarch64: best grp 570.6 vs q1:12 610.4 QPS, and grp:1 is −39 % vs serial. The mean audience group at nq=8 is 1.40 queries, where nq=2–3 calls cost 2–5× per query. Default pool size is 12, chosen on seed 7 and held on seed 42. T=20 loses 14–17 %. Filtered searches intersect the allowlist inside each (query, shard) task: 3.7 ms / 2.2 ms, against 6.4–7.0 / 3.9–4.4 ms up front on the caller. The results are bit-identical to serial, including tie order. | 3a, 3b, critic, p3 |
| D4 | R1 framing | Ships first, standalone. It is a correctness/availability fix (a shared SQLite connection used from threadpool threads), not an auth bypass. | critic |
| D5 | orjson | Move to a `bench` dependency group, no bump. Runtime JSON stays stdlib (ADR 0001:31). | 4a, critic |
| D6 | Free-threading status | Evaluation-only experimental image after items 1–3. Not the default until turbovec publishes cp314t wheels. raggio never maintains a turbovec fork. | critic |
| D7 | Upstream posting | Drafts plus scratch repros kept **outside the repo** in `D:/DEV/_scratch/upstream-drafts/`. The maintainer reviews and posts them. | 3b |
| D8 | Batched-sync failure policy | A job stays `processing`, payload kept, until the index sync covering it lands. Batches are bounded by N jobs or T ms. Flush in `stop()`. Crashes recover through the existing replay. | 2a, critic |
| D9 | Covering-index migration | **Measured.** CREATE INDEX takes 40.8 s true-cold and 1.73 s warm at 2.55M, about 16 s per 1M chunks cold; DROP `idx_records_doc` takes 1.9 s; meta.db grows by 96 MB, with an 86 MB freelist left behind. It is a one-time migration inside `open_meta_db`, so it runs inside the off-loop `Collection()` construction and logs its progress. Ship a release note. No lazy background build. **The covering index changes one query plan**: the type-filtered listing sorted by `json_extract(metadata)` gets 3× slower warm, and `ANALYZE` does not fix it. C pins that query with `NOT INDEXED` or unary `+`, and adds an EXPLAIN-plan regression test. | 2b, critic, p1 |
| D10 | Tokenizer parity | The native tokenizer replicates Python's current `[^\W_]+` (str.isalnum semantics) plus `_fold` NFKD folding exactly, including the Indic splitting. Moving toward FTS5 unicode61 is a separate, measured change. Parity is **bit-identical by `repr`**, which is stricter than 1e-9 and was met by the aarch64 prototype on 914 queries. The prune path is covered separately by D14. | 1, critic, p2 |
| D11 | CI | A minimal `tests.yml`: 3.12 running the Python-fallback suite, plus a native job once E lands. A non-blocking 3.14t job with the GIL guard is added by G. | critic |
| D12 | True-cold measurement | Evict the collection's files from the page cache with `os.posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)` on every file in the data dir. Verify the eviction with `fincore`/mincore. Run it via `podman unshare`. No sudo or reboot is needed. **Validated in probe p1**: true-cold first GET is 120–125 s (flat and IVF), against 128 s after the reboot. | DGX baseline, p1 |
| D13 | Memory release | Set `ENV MALLOC_TRIM_THRESHOLD_=134217728` in the image, and call `malloc_trim(0)` (ctypes, glibc only, no-op elsewhere) after every index job. No `MALLOC_ARENA_MAX`. Run `_require_headroom` **before** the backfill/COUNT pre-work, and raise the attach need to the measured growth (about 2.2 GiB). Default glibc is **OOM-killed** by `POST /index` at 2.55M on a swapless 4 GiB cgroup (2/2 runs), and the guard had passed. The release gate is a swapless run (`--memory 4g --memory-swap 4g`). Owner C, including the one ENV line in the Dockerfile. | p1 |
| D14 | Prune tokenizer | `_prune_common` switches from `re.findall(r'\w+')` to the stage-2 tokenizer (`[^\W_]+` + `_fold`), so all three token paths agree on `_`. This is a **ranking change**: top-10 is identical on 98.2 % of bench queries, and hit@10 is unchanged. It is the only lever on hybrid p99 (warm ~541 → ~117 ms). Owner E, as its own task and its own DGX A/B arm, landed before the native arm, and never folded into it. | p2 |
| D15 | SQLite build | Concurrent hybrid is capped by SQLite's global memstatus malloc mutex, not only by the GIL. On Debian SQLite 3.40.1, SQL alone falls from 36.5 q/s at T=2 to 16.8 at T=8. D2's PBS interpreter bundles SQLite 3.50.4, which scales better: native is 2.1× Python. Memstatus cannot be switched off there (`sqlite3_config` is not exported). Consequences: A's base migration is itself a performance change, so A records a **new reference baseline** after it and does not claim equivalence on hybrid rows. Every DGX row records `sqlite3.sqlite_version`. E's concurrent-QPS target is reported, not gated. G's verdict must account for the SQLite mutex. Building a custom SQLite is out of scope. | p2 |
| D16 | BLAS threads | Set `ENV OPENBLAS_NUM_THREADS=1` in the image. By default, numpy's scipy-openblas starts 20 busy-spinning threads under any search load. They burn about 16 cores and push query and event-loop threads onto the A725 cores (serial 4 vs 8 ms, filtered 32 vs 70 ms). This is the main DGX noise source, and it caps F's server concurrency: about 203 QPS with default BLAS, about 325 with the setting. Owner A, as part of the base migration, so the one new reference baseline (D15) absorbs it. A's DGX step checks that the IVF build (k-means + attach) and the ingest-free rows do not regress beyond the band; p3 did not measure these. If the build regresses, the fallback is to limit BLAS threads on the search path only with threadpoolctl, still owned by A. | p3 |

## 4. Cross-plan contracts (ownership — only the owner changes the thing)

### 4.1 Code ownership

| Surface | Owner | Others must |
|---|---|---|
| `bench/bench.py`, `bench/dgx/*.sh` runbook | A | Add new probe files only (`bench/<name>_probe.py`); never edit bench.py. |
| `pyproject.toml` groups, `uv.lock`, `required-version` | A first. E then adds the native path source and extra. G adds nothing to the default lock. | Re-lock only in their own task, after A has merged. |
| `Dockerfile` | A (base migration and `ENV OPENBLAS_NUM_THREADS=1`, D16), C (the single `ENV MALLOC_TRIM_THRESHOLD_` line, D13), E (Rust builder stage for `raggio-native`), G (3.14t/turbovec sdist) | E's builder builds with the uv-installed `/python` (no python3 exists in `rust:*-slim`), on the same Debian release as the runtime. Use `set -o pipefail` inside `RUN`, because podman's OCI format ignores `SHELL`. |
| `.github/workflows/tests.yml` | A creates it. E adds the native job, G the 3.14t job. | — |
| `CollectionManager.get_config` / `list_collections` / `delete_collection` locking | B | C's off-loop construction builds on B's locking. |
| `Collection.stop()` | B puts the close under `db_lock` and after the RW lock write-acquire. | D2 adds its sync flush inside that same critical section, before the connections close. |
| `indexed_counts` | B makes it copy-on-write: writers build a new dict and rebind; readers take one local reference. | D moves the update after commit, keeping copy-on-write. E receives `N` as a parameter. |
| `open_meta_db` indexes | C. `idx_records_doc_type ON records(doc_id, type, indexed)` replaces `idx_records_doc`, and a partial `idx_jobs_open ON jobs(id) WHERE status IN ('pending','processing')`. | D's claim query, `resume_pending` and `pending_jobs` keep the literal predicate `status IN ('pending','processing')`. |
| `_reconcile_ghosts`, `stats()`, `CollectionManager.touch()` construction, `_vec_sample`, `_require_headroom` / memory release | C | — |
| Job journal format (`job_payloads` side table, legacy TEXT JSON accepted on replay), `enqueue`, `_run_worker`, `_upsert_rows`, `_sync_index` batching | D | — |
| `_bm25_rescore`, `_fold_tokens` fast path, `_prune_common` tokenization (D14), `raggio_native` package (`native/`) | E | Keep the Python implementations as the reference and fallback. |
| `_backfill_vecs` and the attach/detach pre-work order (guard first) | C | — |
| `_IvfIndex` (search, remove, sync), `_vector_ids`, `_allow_cache` | F | D's batched sync calls F's dirty-shard API if F merged first; otherwise it calls the existing `_sync_index`. |
| `/healthz` payload | E adds `"bm25": "native"|"python"`. G adds `"gil_enabled": bool`. | Additive keys only. |

### 4.2 Names that cross plan boundaries

- **Deployment knobs:** unprefixed environment variables read in `raggio.config.Settings`, following the existing style, and passed down through `CollectionManager` into `Collection`. Algorithm constants stay module-level in `store.py`, where tests monkeypatch them.
  - `NATIVE_BM25`: `auto` by default (native if importable) or `0` to force Python. Owner E.
  - `IVF_SEARCH_THREADS`: default `min(12, os.cpu_count())` (D3, p3); `1` means the serial path. Owner F. Never set `RAYON_NUM_THREADS=1`: it flattens turbovec's pooled path to about 1,440 calls/s.
  - `OPENBLAS_NUM_THREADS=1` is an image `ENV` (D16, owner A), not a `Settings` knob.
  - `SYNC_BATCH_JOBS` and `SYNC_BATCH_MS`: owner D.
  - `GC_FREEZE`: owner G.
- **Native module:** the import name is `raggio_native`, the distribution name `raggio-native`, and the crate lives in `native/`. It is built without abi3 and declares `gil_used = false`.
- **New tests:** each plan adds its own module (`tests/test_<plan-topic>.py`). Plans don't reorganize existing test files.

### 4.3 Invariants every plan must keep (ADR 0001/0002/0003)

- **Single writer:** every write transaction runs wholly inside `db_lock`, in a worker thread, never on the event loop.
- **Reads:** loop-side reads use per-thread read connections.
- **auto_vacuum:** `PRAGMA auto_vacuum=INCREMENTAL` comes before `journal_mode=WAL`, and `incremental_vacuum` is drained with `.fetchall()`.
- **Calibration:** calibrate early or never (CAL_THRESHOLD 10k, reservoir rules as in ADR 0001).
- **Ghost reconciliation:** it only evicts index ids that have no DB row, and never re-adds.
- **Job journal:** a job is `done` only after the index sync covering it.
- **Scoring:** fp16 exact rescore (ADR 0003), and two-stage BM25 with tie-break key `(-score, rid)`.
- **Memory:** the cgroup headroom guard refuses the job rather than letting the container OOM.

## 5. New findings from the 2026-09-28 DGX baseline attempt (image 19a8a2e)

The first baseline attempt ran 01:36–02:07 and failed. Baseline3 (03:14–03:38) succeeded after
three workarounds, all applied to the DGX copy of the bench only: a 900 s reuse-check timeout,
the arXiv fingerprint, and the IVF detach in an 8 GiB container. The findings are assigned below.

1. **Cold load on the loop.** `CollectionManager.touch()` constructs `Collection(...)` synchronously on the event loop (store.py:1710). The first `GET /collections/bench` after a start took **128 s** on the rebooted DGX (IVF, 4 GiB, mostly-cold page cache), and `/healthz` and every other request block for the whole load. Owner C: build it with `asyncio.to_thread`. The asyncio primitives created in `__init__` must be safe to create off-loop; verify each one.
2. **`stats()` on the loop.** `stats()` does a full `records` scan and `COUNT(DISTINCT doc_id)` synchronously on the loop (store.py:1579, app.py:146-150). Under 4 GiB, **every** `GET /collections/{name}` pays this: about 41 s each time (p1), because 3.57 GB of records cannot stay in the cgroup's page cache. Owner C: move it to a thread and make it cheap using the covering index. The p1 prototype measured 0.57 s. Also audit the other request paths that scan `records`, such as filtered/sorted listings and filtered allowlists. They take 43–54 s cold at 4 GiB and must stay off the loop. An expression index for them is a follow-up, not part of C.
3. **Bench fragility.** Owner A.
   - `bench.py`'s `current_count` uses a 30 s httpx timeout. A timeout is read as "no reuse", so the bench rebuilds or re-ingests.
   - The committed `bench/fingerprint-raggio.json` belongs to the *email* corpus. With `cp --update=none` it survived, so the raggio engine would DELETE the collection and re-ingest.
   - A reuse check that cannot reach the server must fail loudly. It must never re-ingest.
   - Fingerprints must be keyed by corpus/limit, or be untracked.
   - The bench must wait for `pending_jobs == 0` before measuring.
4. **RSS retained after an index build.** After the replayed attach job built the IVF index, VmRSS stayed around 3 GB. Baseline3 measured 2.72 GB right after a 4 GiB build, against 1.51 GB for the same index loaded from disk. The IVF → flat detach is refused at 4 GiB even from a fresh load, and needed an 8 GiB container (285 s). The next build (job 10203) was refused with `index build needs ~1884 MB free memory, container has ~1020 MB`. `_cgroup_mem_free` is `memory.max − VmRSS`, so freed-but-retained glibc heap counts as used. p1 shows a worse problem: with default glibc, a swapless 4 GiB container is OOM-killed during the build, after the guard passed. Owner C: apply D13 (trim threshold + `malloc_trim(0)`), move the guard ahead of the pre-work, and use the measured need. The guard stays conservative; the swapless run is the gate.
5. **Replay cost.** On a cold 4 GiB container, replaying the pending attach jobs took about 20 min for two builds, against 337 s for one build in the original run. p1 breaks down a true-cold `POST /index` (556 s, against 52 s warm):
   - backfill LEFT JOIN + COUNT pre-work: 178 s;
   - `_vec_sample` `ORDER BY RANDOM()`: 152 s. It reads 16.9 GB, and rowid sampling cuts it to 8 s;
   - stream+add: 170 s;
   - swap live-set scan: 55 s.

   Owner C: rowid sampling, a `_backfill_vecs` that avoids the full `records` scan, and the swap scan on the covering index. The DGX runbook times one cold attach.

## 6. DGX benchmark protocol (all plans)

The host is `gn100`, an NVIDIA DGX Spark: GB10, 20 aarch64 cores, about 120 GB RAM, rootless
podman 4.9.3, uv on the host, and **no Rust toolchain** (native code builds only inside a
container build stage).

Access: `ssh -F "C:/Users/emanu/AppData/Local/NVIDIA Corporation/Sync/config/ssh_config" -o BatchMode=yes gn100`.

**Any DGX run over about 5 minutes needs the user's explicit go-ahead in the session, with a time cap.**
A plan's DGX task is written as runnable steps, but its first step is "ask the user for a
run window".

- **Deploy:** `git archive HEAD | ssh … 'rm -rf ~/raggio && mkdir ~/raggio && tar -x -C ~/raggio'`. Then:
  - symlink the corpus: `ln -sfn ~/turborag/bench/corpus ~/raggio/bench/corpus`;
  - copy `~/turborag/bench/gt-2549619-42-d1024.npz` into `~/raggio/bench/`;
  - run `~/.local/bin/uv sync --frozen --group bench`;
  - run `podman build -t localhost/raggio:<sha> .`.
- **Container:** `podman run -d --name bench-tv --memory 4g -p 18000:8000 -v bench-tv:/data -e ROOT_API_KEY=bench localhost/raggio:<sha>`. The collection name is `bench`.
  - `podman rm -f` may only name `bench-tv`, never `bench-wv` (the Weaviate container, whose `bench-wv` volume must be preserved).
  - Every run starts from a fresh container of the image under test.
- **Bench invocation:** `.venv/bin/python -u bench/bench.py --limit 2549619 --caps-note "<CAPS>" --host "<HOST>" --engine raggio|raggio-ivf --out <file>`.
  - Use **`--limit 2549619`**. 2,549,119 is the ingested row count, and passing it forces a 31-min reingest and a different query set.
  - Never pass `--reingest` except in an ingest-measurement run, which comes last because it rewrites and compacts files.
- **Order in a session:**
  1. Record `uptime`, `free -m`, the Cached line of `/proc/meminfo`, `podman ps -a`, the image digest, the git SHA and the turbovec version.
  2. First container start: record the time to `/healthz` and to the first `GET /collections/bench`.
  3. Wait for `pending_jobs == 0`.
  4. A discarded warm-up run0 per engine.
  5. Measured runs. Group runs by index state, so there is only one IVF ↔ flat transition per session.
     - Until Plan C lands, the detach needs a transient 8 GiB container.
     - The IVF build from flat works at 4 GiB and is measured.
     - Reference script: `D:/DEV/_scratch/dgx/dgx-baseline3.sh`, which Plan A turns into `bench/dgx/`.
- **Noise band:**
  - Flat: run1 vs run2.
  - IVF: at least 3 measured runs, reporting the median, with max − min as the band. Warm IVF runs vary 16–24% on concurrent QPS and filtered p50.
  - Vector concurrent QPS is bimodal (120–225) across identical configs, so it always needs at least 3 runs.
  - The band must include **between-server** variance: every A/B arm runs on a fresh container. Identical code measured 6–7% apart at IVF warm p50 and ≤1.3% apart on flat (p2).
  - The main source of IVF noise is the OpenBLAS spinners and X925/A725 thread placement (p3, D16). Both arms of an A/B run with the same `OPENBLAS_NUM_THREADS`, and every row records it. Rootless podman cannot pin with `--cpuset-cpus`. When a single-thread latency needs attributing, pin in-process with `os.sched_setaffinity` (X925 = cpus 5–9, 15–19).
  - Claim a win only if it exceeds the band.
  - Report the concurrent phase as repeating the same 500 queries (warm caches).
- **Regime labelling:**
  - Every reported row states its page-cache regime: true-cold (fadvise-evicted, D12) or host-warm.
  - Every row also states its memory cap and `sqlite3.sqlite_version` (D15).
  - Arms of one A/B share the same regime.
  - Pages cached by dead cgroups escape the 4 GiB cap, which is why host-warm bench runs are faster than true-cold ones.
  - Report the first hybrid pass after a load separately: its first ~10 queries take 470–980 ms.
- **Memory gates:** on gn100, `--memory 4g` also grants 4 GiB of swap from the host's 16 GiB swap file. Any memory claim (D13) is measured with `--memory 4g --memory-swap 4g`.
- **Cold start** is claimed only from a true-cold start (reboot or D12 fadvise eviction), never from within an A/B session.
- **Tunables** (pool size, batch N/T, candidate depth) are chosen on an independent sample (seed 7, the `bench/text_probe.py` precedent, ADR 0003) and published on seed 42.
- **Log and background discipline:**
  - launch long runs with `setsid -f` into a log file;
  - poll the log;
  - never `pkill -f` a pattern that matches the polling command itself;
  - never stop or delete the user's DGX work unasked.

## 7. Acceptance, per item (DGX, against the re-baselined preceding main)

- **E (prune-tokenizer arm, D14):**
  - text-hit@10 unchanged;
  - ranked-OR match counts within the posting budget for the underscore titles;
  - hybrid p99 (warm pass) better beyond the band.
  - The changed top-10s are reported: expect about 1.8 % of queries.
- **E (native arm):**
  - bit-identical scores by `repr`, same ids and tie order, on the parity goldens and on the 500 bench queries (text-hit@10 unchanged);
  - flat hybrid p50 better beyond the noise band. The p2 estimate is 0.71–0.79×, against a band of ≤2.8 %;
  - `NATIVE_BM25=0` restores today's behaviour bit-for-bit.
  - Concurrent QPS and p99 are reported, not gated (D15).
- **F:** gn100, 2.55M IVF (nlist 256, nprobe 16), `--memory 4g`, warm, `OPENBLAS_NUM_THREADS=1` in both arms. The p3 prototype value is in brackets, against base with OPENBLAS=1.
  1. recall@10 unchanged at the default nprobe [0.993]. Ids and float32 score bits are identical to `IVF_SEARCH_THREADS=1` on seed 42, unfiltered and filtered, including tie order.
  2. Concurrent QPS (c=8, median of at least 3 runs) is at least 1.5× the re-baselined main, and F's minimum is above base's maximum. p3 measured 324.7 [308.6..381.4] against 189.2 [162.8..207.3].
  3. Filtered p50 ≤10 ms [6.41, against 36.6].
  4. Serial p50 better beyond the band [6.50, against 8.44].
  5. Container memory peak within 50 MB of base [1.82 GB in both].
  6. `IVF_SEARCH_THREADS=1` reproduces the serial path.
  7. The run reports the largest IVF list size, against the 32,768-row pooled-path cliff [30,443].
  - After F, the server limit is the HTTP/event-loop pipeline, about 3.1 ms per request, against an in-process ceiling of 610–662 QPS. Follow-ups outside A–G: two `_drain_scans` batches in flight, and `_hydrate` off the loop.
- **C:** gn100, 2.55M, `--memory 4g`, true-cold unless stated. The p1 prototype value is in brackets.
  1. Container start → first GET: ≤6 s IVF [4.32], ≤5 s flat [3.34].
  2. `/healthz` max during a cold load ≤0.1 s [0.003].
  3. Later GET under 4 GiB ≤1.0 s [0.57].
  4. `Collection()` cold read ≤ index size + 150 MB.
  5. Migration:
     - CREATE INDEX ≤60 s cold [40.8] and ≤3 s warm [1.73];
     - meta.db growth ≤120 MB [+96].
  6. `_vec_sample(65536)`:
     - ≤15 s cold [8.05];
     - ≤1 s warm [0.39].
  7. Swapless memory run (`--memory-swap 4g`), POST → DELETE → POST index:
     - no OOM;
     - both second operations pass the guard;
     - attach VmHWM ≤3.8 GiB [3.63–3.67];
     - post-job RSS within 0.5 GiB of loaded.
  8. Warm "list page sorted chunks" ≤1.0 s [0.76 pinned; 2.73 unpinned].
  9. Cold `POST /index` pre-work ≤10 s [baseline 178].
- **D:** ingest vec/s beyond the noise band on the IVF and flat paths, and no lost or unsynced job under the crash/replay tests.
- **B:** stress test green 50/50 runs; no performance claim.
- **G:** the full test suite green on 3.14t with the GIL guard. QPS/latency ABAB on one boot. The report states whether to continue; making it the default is out of scope.

## 8. DGX probe evidence index (2026-09-28, gn100)

| Probe | Report | Drives |
|---|---|---|
| Baseline3 | `research/2026-09-28-dgx-baseline.md` | §1 numbers, §6 protocol |
| p1 cold start / SQLite / memory | `research/2026-09-28-dgx-probe-p1-coldstart-sqlite-memory.md` | D9, D12, D13, §5.1–5.5, §7 C |
| p2 hybrid stages / native aarch64 | `research/2026-09-28-dgx-probe-p2-hybrid-native.md` | D10, D14, D15, §4.1 Dockerfile notes, §7 E |
| p3 IVF fan-out variants | `research/2026-09-28-dgx-probe-p3-ivf-fanout.md` | D3, D16, §4.2 `IVF_SEARCH_THREADS`, §6 noise source, §7 F |
| p4 3.14t aarch64 build | `research/2026-09-28-dgx-probe-p4-freethreaded.md` (**pending**: not run yet) | D6, §7 G |

The probe prototypes (`store_planc.py`, the p2 native crate) are evidence only. The plans carry the real code.
