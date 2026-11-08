# Sibling-vector reevaluation recordings

Current-main baseline: `37c686e`; historical candidate: `614c4b8`.
Runtime image source: `f9a6561`. The single-process HTTP harness correction is
`81b7490`; `bench/sibling_reeval_single.sh` records the final launch procedure.

The report and interpretation are in
`docs/research/sibling-vector-reevaluation-2026-11-08.md`, with the protocol next to
it and ADR 0019. These are evaluation records, not a deployment decision.

## Files

| Recording | Purpose |
|---|---|
| `wiki-aa-single.json.gz` | Primary same-process baseline/baseline calibration, 7 pairs, 2-second blocks, concurrency 1 and 8 |
| `wiki-ab-single.json.gz` | Primary Wikipedia baseline/candidate comparison, all 10 query shapes |
| `wiki-controls-ab.json.gz` | Separate longer ordinary summary/document controls, 7 pairs, 8-second blocks at concurrency 8 |
| `wiki-controls-aa-clean.json.gz` | Clean calibration for the longer controls |
| `synthetic3072-ab.json.gz` | Separate 3072-dimension repeated-document sensitivity and ranked-summary control |
| `wiki-controls-aa.json.gz` | **Contaminated** long A/A: unrelated host load near 94%; excluded from performance/noise analysis |
| `wiki-aa.json.gz` | Initial between-process diagnostic: not the primary causal comparison |
| `primary-analysis.json` | Conservative noise/gate classification of the primary results |
| `control-followup-analysis.json` | Longer-control gate analysis using the clean replacement calibration |
| `summary.json` | All phase summaries, coverage, request counts and exclusion metadata |
| `*-profile.json` | Offline SQL/vector-fetch counts, temporary Python allocations and response hashes |
| `*-facts.json` | Source/method hashes, interpreter, libraries and native scorer identity |
| `wiki-corpus-meta.json` | Wikipedia dataset revision, embedding model and original preparation settings |
| `validation.json`, `linux-native.xml` | Test counts and Linux arm64 JUnit evidence |
| `raw-manifest.json` | SHA-256 hashes of the decompressed recordings, file sizes and exclusions |

Gzip files retain every paired round, per-request latency samples, sample counts,
server CPU/RSS and host CPU observations. Compression is verified lossless. Results
use paired medians and paired-bootstrap intervals; request samples are not treated
as independent benchmark runs. Retained RSS in one process is not an independent
per-arm memory comparison.

Reproduce the primary gate calculation from the repository root:

```bash
uv run --group bench python bench/sibling_reeval_summary.py \
  --aa bench/sibling-reevaluation-2026-11-08/wiki-aa-single.json \
  --ab bench/sibling-reevaluation-2026-11-08/wiki-ab-single.json \
  --output analysis.json
```

The reader automatically uses `.json.gz` when the uncompressed file is absent.
For the longer-control analysis, substitute `wiki-controls-aa-clean.json` and
`wiki-controls-ab.json`. Keep contaminated/calibration-diagnostic files separate.
