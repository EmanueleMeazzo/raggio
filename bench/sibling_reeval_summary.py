"""Analyze paired rounds without treating individual requests as independent runs."""
import argparse
import gzip
import json
from pathlib import Path


def read(path):
    if path.exists():
        return json.loads(path.read_text())
    with gzip.open(str(path) + ".gz", "rt", encoding="utf-8") as stream:
        return json.load(stream)


def percentile(values, p):
    values = sorted(values)
    position = (len(values) - 1) * p / 100
    lo = int(position)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (position - lo)


def analyze(aa, ab):
    for result in (aa, ab):
        if result.get("response_parity") is not True:
            raise ValueError("incomplete or failed response-parity gate")
        if len(result["runs"]) != len(result["summary"]):
            raise ValueError("incomplete paired results")
        if any(len(rounds) != result["pairs"] for rounds in result["runs"].values()):
            raise ValueError("missing paired rounds")
    noise = {}
    for key, rounds in aa["runs"].items():
        noise[key] = {metric: percentile([abs((row["b"][metric] / row["a"][metric] - 1) * 100)
                                         for row in rounds], 95)
                      for metric in ("p50_ms", "server_cpu_ms", "qps")}
    gates = {}
    for key, summary in ab["summary"].items():
        concurrency = key.rsplit("/", 1)[1]
        matched = key if key in noise else (
            "doc-vector-k10-ranked/" + concurrency if key.startswith("doc-vector") else None)
        envelope = noise[matched] if matched else {
            metric: max(values[metric] for name, values in noise.items() if name.endswith("/" + concurrency))
            for metric in ("p50_ms", "server_cpu_ms", "qps")}
        metrics = {}
        for metric, tolerance in envelope.items():
            result = summary[metric]
            delta = result["change_pct"]
            lo, hi = result["paired_median_95_ci_pct"]
            gain = delta > tolerance and lo > 0 if metric == "qps" else delta < -tolerance and hi < 0
            regression = delta < -max(3, tolerance) and hi < 0 if metric == "qps" else delta > max(3, tolerance) and lo > 0
            metrics[metric] = {"change_pct": delta, "ci_pct": [lo, hi], "aa_envelope_pct": tolerance,
                               "gain_beyond_noise": gain, "regression_gate_failure": regression}
        gates[key] = metrics
    count = sum(row[arm]["requests"] for result in (aa, ab) for rounds in result["runs"].values()
                for row in rounds for arm in row)
    busy = max(row[arm]["host_busy_fraction"] for result in (aa, ab) for rounds in result["runs"].values()
               for row in rounds for arm in row)
    return {"requests": count, "max_host_busy_fraction": busy, "aa_envelopes": noise, "gates": gates}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--aa", type=Path, required=True)
    parser.add_argument("--ab", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(read(args.aa), read(args.ab))
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for name, metrics in result["gates"].items():
        print(name, {metric: {"delta": round(value["change_pct"], 2),
                             "gain": value["gain_beyond_noise"], "regression": value["regression_gate_failure"]}
                     for metric, value in metrics.items()})
