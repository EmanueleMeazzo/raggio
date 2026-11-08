"""Fixed external HTTP client: paired A/A and A/B, server CPU/RSS, exact parity."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import time

import httpx

KEY = "sibling-evaluation"


def process_cpu(pid):
    text = Path(f"/proc/{pid}/stat").read_text()
    fields = text[text.rfind(")") + 2:].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def process_rss(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("server RSS unavailable")


def host_cpu():
    fields = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
    return sum(fields), fields[3] + fields[4]


def percentile(values, p):
    ordered = sorted(values)
    at = (len(ordered) - 1) * p / 100
    lo = int(at)
    return ordered[lo] + (ordered[min(lo + 1, len(ordered) - 1)] - ordered[lo]) * (at - lo)


def interval(values):
    rng = random.Random(821)
    samples = [statistics.median(rng.choices(values, k=len(values))) for _ in range(5000)]
    return [percentile(samples, 2.5), percentile(samples, 97.5)]


async def run(args):
    servers = json.loads(Path(args.servers).read_text())
    manifest = json.loads(Path(args.queries).read_text())
    selected = args.cases.split(",") if args.cases else list(manifest["cases"])
    arms = ("a", "b") if args.mode == "aa" else ("a", "c")
    bodies = {name: [json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
                     for body in manifest["cases"][name]] for name in selected}
    expected = {}
    coverage = {}
    headers = {"x-api-key": KEY, "content-type": "application/json"}
    output = {"mode": args.mode, "fixture": {k: v for k, v in manifest.items() if k != "cases"},
              "benchmark_client_sha256": hashlib.sha256(Path(__file__).read_text(encoding="utf-8").encode()).hexdigest(),
              "client_cpu_affinity": sorted(os.sched_getaffinity(0)),
              "python": sys.version, "platform": platform.platform(), "httpx": httpx.__version__,
              "pairs": args.pairs, "minimum_seconds_per_arm": args.seconds,
              "servers": servers, "coverage": coverage, "runs": {}, "summary": {}}
    for arm in arms:
        pid = servers[arm]["pid"]
        process_cpu(pid)
        process_rss(pid)
    async with httpx.AsyncClient(headers=headers, timeout=120,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=32)) as client:
        async def select(arm):
            if "variant" in servers[arm]:
                response = await client.post(servers[arm]["url"] + "/__sibling_evaluation/arm",
                                             json={"arm": servers[arm]["variant"]})
                response.raise_for_status()
                if response.json()["arm"] != servers[arm]["variant"]:
                    raise RuntimeError("server arm selection failed")

        for arm in arms:
            ready = False
            for _ in range(120):
                try:
                    r = await client.get(servers[arm]["url"] + "/healthz")
                    ready = r.status_code == 200 and r.json()["status"] == "ok"
                except httpx.TransportError:
                    pass
                if ready:
                    break
                await asyncio.sleep(.5)
            if not ready:
                raise RuntimeError(f"server {arm} did not become healthy")
        for name, raws in bodies.items():
            hashes, counts = [], []
            for raw in raws:
                responses = []
                for arm in arms:
                    await select(arm)
                    r = await client.post(servers[arm]["url"] + "/collections/fixture/search", content=raw)
                    r.raise_for_status()
                    responses.append(r.content)
                if responses[0] != responses[1]:
                    raise AssertionError(f"preflight response mismatch: {name}")
                hashes.append(hashlib.sha256(responses[0]).digest())
                hits = json.loads(responses[0])["hits"]
                doc_counts = Counter(h["doc_id"] for h in hits)
                counts.append({"hits": len(hits), "documents": len(doc_counts),
                               "repeated_document_hits": sum(v for v in doc_counts.values() if v > 1)})
            expected[name], coverage[name] = hashes, counts

        async def block(arm, name, concurrency):
            await select(arm)  # outside CPU/wall clocks, with prior requests drained
            url, pid = servers[arm]["url"], servers[arm]["pid"]
            raws = bodies[name]
            latencies = []
            number = [0]
            running = [True]
            rss = [process_rss(pid)]

            async def sample_rss():
                while running[0]:
                    rss.append(process_rss(pid))
                    await asyncio.sleep(.025)

            server_start, client_start = process_cpu(pid), time.process_time()
            host_start = host_cpu()
            start = time.perf_counter()
            sampler = asyncio.create_task(sample_rss())

            async def worker():
                while time.perf_counter() - start < args.seconds:
                    i = number[0] % len(raws)
                    number[0] += 1
                    before = time.perf_counter()
                    response = await client.post(url + "/collections/fixture/search", content=raws[i])
                    latencies.append((time.perf_counter() - before) * 1000)
                    response.raise_for_status()
                    if hashlib.sha256(response.content).digest() != expected[name][i]:
                        raise AssertionError(f"load response mismatch: {arm}/{name}/{i}")

            try:
                await asyncio.gather(*(worker() for _ in range(concurrency)))
            finally:
                running[0] = False
                rss.append(process_rss(pid))
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)
            elapsed = time.perf_counter() - start
            server_seconds = process_cpu(pid) - server_start
            client_seconds = time.process_time() - client_start
            host_end = host_cpu()
            busy = 1 - (host_end[1] - host_start[1]) / max(1, host_end[0] - host_start[0])
            return {"requests": len(latencies), "elapsed_seconds": elapsed,
                    "qps": len(latencies) / elapsed, "p50_ms": statistics.median(latencies),
                    "p95_ms": percentile(latencies, 95), "p99_ms": percentile(latencies, 99),
                    "server_cpu_ms": server_seconds * 1000 / len(latencies),
                    "client_cpu_ms": client_seconds * 1000 / len(latencies),
                    "rss_before_bytes": rss[0], "rss_peak_bytes": max(rss),
                    "host_busy_fraction": busy, "latency_samples_ms": latencies}

        for concurrency in map(int, args.concurrency.split(",")):
            for name in selected:
                key = f"{name}/c{concurrency}"
                rounds = []
                for arm in arms:
                    await block(arm, name, concurrency)
                for pair in range(args.pairs):
                    row = {}
                    for arm in (arms if pair % 2 == 0 else arms[::-1]):
                        row[arm] = await block(arm, name, concurrency)
                    rounds.append(row)
                    output["runs"][key] = rounds
                    Path(args.output).write_text(json.dumps(output), encoding="utf-8")
                summary = {}
                for metric in ("qps", "p50_ms", "p95_ms", "p99_ms", "server_cpu_ms", "rss_peak_bytes"):
                    changes = [(row[arms[1]][metric] / row[arms[0]][metric] - 1) * 100
                               for row in rounds if row[arms[0]][metric] > 0]
                    summary[metric] = {"change_pct": statistics.median(changes),
                                       "paired_median_95_ci_pct": interval(changes),
                                       "baseline_median": statistics.median(row[arms[0]][metric] for row in rounds),
                                       "comparison_median": statistics.median(row[arms[1]][metric] for row in rounds)}
                output["summary"][key] = summary
                Path(args.output).write_text(json.dumps(output), encoding="utf-8")
                print(json.dumps({"case": key, "summary": summary}), flush=True)
    output["response_parity"] = True
    Path(args.output).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--servers", required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("aa", "ab"), required=True)
    parser.add_argument("--cases")
    parser.add_argument("--concurrency", default="1,8")
    parser.add_argument("--pairs", type=int, default=7)
    parser.add_argument("--seconds", type=float, default=2)
    args = parser.parse_args()
    if args.pairs < 1 or args.seconds <= 0:
        parser.error("positive pair count and duration required")
    asyncio.run(run(args))
