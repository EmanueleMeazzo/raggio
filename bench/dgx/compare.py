"""Noise band and A/B table for DGX sessions (spec §6 "Noise band").

A session directory (~/raggio-bench/<label>/, written by session.sh) holds one
run<N>-<engine>.json per bench run, each {engine: {metric: value}}. run0 is the discarded
warm-up: its query metrics are ignored, but a one-shot metric it measured (the IVF build
of a flat-first session) counts. The measured runs are run1..runN (flat two, IVF three);
per metric the central value is their median and the noise band is max - min, never
narrower than the table's display resolution (1 MB, 0.1 ms, 0.001 recall).

One directory: every counted value, the median and the band. Two (baseline first): both
medians and bands and a verdict, "better" or "worse" only when the medians differ by more
than the wider band; "no band" when a side has a single value; "not claimable" for the
bench's cold-start metric, a warm restart (spec §6: cold start is claimed only true-cold).

Usage: python bench/dgx/compare.py <baseline-dir> [<candidate-dir>]
"""

import json
import re
import statistics
import sys
from pathlib import Path

# (results key, label, format, higher is better): same keys, labels and formats as bench.ROWS
METRICS = [
    ("ingest_s", "Ingest wall time (s)", "{:.0f}", False),
    ("ingest_vps", "Ingest throughput (vec/s)", "{:.0f}", True),
    ("index_build_s", "IVF index build (s)", "{:.0f}", False),
    ("mem_after_ingest_mb", "Memory after ingest (MB)", "{:.0f}", False),
    ("mem_under_load_mb", "Memory under query load (MB)", "{:.0f}", False),
    ("disk_mb", "Disk footprint (MB)", "{:.0f}", False),
    ("lat_p50", "Search p50 (ms)", "{:.1f}", False),
    ("lat_p95", "Search p95 (ms)", "{:.1f}", False),
    ("lat_p99", "Search p99 (ms)", "{:.1f}", False),
    ("qps_serial", "QPS serial", "{:.0f}", True),
    ("qps_concurrent", "QPS concurrent", "{:.0f}", True),
    ("lat_c_p95", "p95 under concurrency (ms)", "{:.1f}", False),
    ("lat_filtered_p50", "Filtered p50 (ms)", "{:.1f}", False),
    ("lat_filtered_p95", "Filtered p95 (ms)", "{:.1f}", False),
    ("recall_at_10", "Recall@10 vs exact", "{:.3f}", True),
    ("hybrid_p50", "Hybrid p50 (ms)", "{:.1f}", False),
    ("hybrid_p95", "Hybrid p95 (ms)", "{:.1f}", False),
    ("hybrid_p99", "Hybrid p99 (ms)", "{:.1f}", False),
    ("hybrid_qps_serial", "Hybrid QPS serial", "{:.1f}", True),
    ("hybrid_qps_concurrent", "Hybrid QPS concurrent", "{:.1f}", True),
    ("hybrid_text_hit_rate", "Hybrid text-hit@10", "{:.3f}", True),
    ("cold_start_s", "Cold start to first query (s)", "{:.1f}", False),
]
HIGHER_IS_BETTER = {key: hib for key, _, _, hib in METRICS}
RESOLUTION = {key: 10.0 ** -int(re.search(r"\.(\d)f", fmt).group(1)) for key, _, fmt, _ in METRICS}
ONE_SHOT = {"ingest_s", "ingest_vps", "index_build_s"}  # measured once, possibly by the warm-up
NOT_CLAIMABLE = {"cold_start_s"}
ENGINE_ORDER = ["raggio", "raggio-ivf", "weaviate"]
RUN_FILE = re.compile(r"run(\d+)-(raggio-ivf|raggio|weaviate)\.json")


def load(session_dir):
    """{engine: {run number: {metric: value}}} from a session directory."""
    runs = {}
    for p in sorted(Path(session_dir).iterdir()):
        m = RUN_FILE.fullmatch(p.name)
        if m:
            data = json.loads(p.read_text(encoding="utf-8"))
            runs.setdefault(m.group(2), {})[int(m.group(1))] = data.get(m.group(2), {})
    if not runs:
        raise SystemExit(f"{session_dir}: no run<N>-<engine>.json files")
    return runs


def values(engine_runs, key):
    """The values that count for one engine's metric, in run order."""
    return [engine_runs[n][key] for n in sorted(engine_runs)
            if key in engine_runs[n] and (n >= 1 or key in ONE_SHOT)]


def summary(vals):
    """(median, max - min) of the counted values."""
    return statistics.median(vals), max(vals) - min(vals)


def verdict(key, base, cand):
    if key in NOT_CLAIMABLE:
        return "not claimable"
    if len(base) < 2 or len(cand) < 2:
        return "no band"
    (base_mid, base_band), (cand_mid, cand_band) = summary(base), summary(cand)
    gain = cand_mid - base_mid
    if not HIGHER_IS_BETTER[key]:
        gain = -gain
    band = max(base_band, cand_band, RESOLUTION[key])
    if gain > band:
        return "better"
    if -gain > band:
        return "worse"
    return "within band"


def _rows(base, cand=None):
    """(engine, key, label, fmt, base values, cand values) for every metric either side has."""
    sessions = [base] + ([cand] if cand is not None else [])
    for engine in [e for e in ENGINE_ORDER if any(e in s for s in sessions)]:
        for key, label, fmt, _ in METRICS:
            b = values(base.get(engine, {}), key)
            c = values(cand.get(engine, {}), key) if cand is not None else []
            if b or c:
                yield engine, key, label, fmt, b, c


def _mid_band(fmt, vals):
    if not vals:
        return ["-", "-"]
    mid, band = summary(vals)
    return [fmt.format(mid), fmt.format(band) if len(vals) > 1 else "-"]


def render(base_dir, cand_dir=None):
    base = load(base_dir)
    cand = load(cand_dir) if cand_dir else None
    if cand is None:
        lines = [f"Noise band: {base_dir}", "",
                 "| Engine | Metric | runs | median | band |", "|---|---|---|---|---|"]
    else:
        lines = [f"A/B: baseline {base_dir} vs candidate {cand_dir}", "",
                 "| Engine | Metric | base median | base band | cand median | cand band | verdict |",
                 "|---|---|---|---|---|---|---|"]
    for engine, key, label, fmt, b, c in _rows(base, cand):
        if cand is None:
            cells = [engine, label, " / ".join(fmt.format(v) for v in b), *_mid_band(fmt, b)]
        else:
            cells = [engine, label, *_mid_band(fmt, b), *_mid_band(fmt, c),
                     verdict(key, b, c) if b and c else "-"]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def beyond_band(base_dir, cand_dir):
    """{"better": [(engine, key)], "worse": [(engine, key)]}: the rows outside the noise band."""
    out = {"better": [], "worse": []}
    for engine, key, _, _, b, c in _rows(load(base_dir), load(cand_dir)):
        v = verdict(key, b, c) if b and c else "-"
        if v in out:
            out[v].append((engine, key))
    return out


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    print(render(*sys.argv[1:]))
    if len(sys.argv) == 3:
        for name, rows in beyond_band(*sys.argv[1:]).items():
            print(f"\n{name} beyond the noise band: {len(rows)} row(s) {rows}")
