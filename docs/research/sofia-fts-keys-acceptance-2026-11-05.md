# Sofia DEV acceptance: metadata title retrieval

Recorded: 2026-11-05. The user supplied the Sofia acceptance report below and
authorized merging all remaining PRs. Its R4 check passed on raggio 0.3.0: all three
tested collections match Weaviate's title-based document retrieval, their record
counts match, and all reported content spot checks pass.

The table transcribes the [source screenshot](assets/sofia-fts-keys-acceptance-2026-11-05.png).
The title column compares reported match counts for **raggio / Weaviate**; it does
not specify a query denominator. Earlier baseline columns retain their original
samples. No acceptance benchmark was rerun when recording this report.

| Collection | Documents / chunks, equal in both systems | Own-title document in top 3: raggio / Weaviate | Before R4, as reported | Spot checks |
|---|---:|---:|---:|---|
| datascience | 2,948 / 40,656 | 20 / 20 | 20 / 20 | 100 checked, 0 mismatches |
| legal_knowhow | 22,339 / 143,179 | 19 / 19 | 11 / 20 | 100 checked, 0 mismatches |
| nickysap | 207 / 1,580 | 20 / 20 | 15 / 20 | 100 checked, 0 mismatches |

## Accepted baseline

- [PR #13](https://github.com/EmanueleMeazzo/raggio/pull/13), native search JSON
  parsing: `a197726311296d77fd12d932be7a594a6c46c08a`, merged by
  `e5d42be8f7740eea3130b427fb344554d59d7ebf`.
- [PR #14](https://github.com/EmanueleMeazzo/raggio/pull/14), immutable metadata
  `fts_keys` throughout BM25: `2939c3c79504d7d477f50a13ea389f1687407159`, merged by
  `2e8082b875faff83a86d0fd37218790f7dbfec06`.
- Published image: `emeadatadevmain.azurecr.io/raggio:0.3.0-2939c3c`, manifest
  `sha256:f52a1e46cae4be0848215ed0bd4643234515c457649a8350c7b0a8a542e1abbf`.
  The screenshot identifies the accepted release as 0.3.0. The merged runtime,
  dependency lock, native code, Dockerfile and tests exactly match PR #14's tree;
  this acceptance report adds documentation only.
- Document collections opt into `["title", "source", "tags"]`. Only the three
  listed collections were recreated in this DEV acceptance. Other DEV collections
  and `user_memories` were not copied in this R4 run; the report assigns their final
  copy to cutover. The handover keeps user memories on vector-only retrieval.
- Both PRs passed all five CI jobs: Python reference, native x86/ARM, and
  experimental free-threaded x86/ARM with concurrency stress. PR #14 passed
  824 local native tests with 4 expected skips, the frozen lock check, strict docs,
  production image build and feature-specific runtime smoke checks.
- Both merge commits use `emanuele.meazzo@gmail.com` for author and committer.

## Deployment details and comparison limits

`legal_knowhow` now uses 256 IVF partitions and searches 32 per query; the reported
rebuild took 54 seconds. Earlier performance baselines used `nprobe=16`, so future
latency comparisons must account for the changed index configuration.

The copy deliberately excluded 123 re-keyed duplicates (reported as `123+0+0`),
39 orphan summaries in datascience, and five chunk groups lacking a main summary:
67 chunks across datascience and legal_knowhow. The Sofia report states those records
are unreachable through current search. The equal counts and spot checks describe
the resulting searchable corpus.

This is deployment evidence for retrieval quality and copy integrity. It supplies
no search latency, throughput, CPU or memory measurements. [ADR 0015](../adr/0015-metadata-fts-keys.md)
retains the local indexing costs and controls; [ADR 0016](../adr/0016-native-search-json.md)
retains native parsing's repeatable local gains. The [PR #11 performance report](sofia-scoped-search-baseline-2026-11-05.md)
remains historical evidence for its measured runtime and settings. Future retrieval
experiments start from the accepted 0.3.0 runtime and record corpus, keys, index
configuration and cache history explicitly.
