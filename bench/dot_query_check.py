"""Micro-probe for the ADR 0020 addendum: what the search handler's check of a "dot"
query costs as _dot_unit (the float64 copy, norm and division, the result dropped)
and as _dot_check (element-wise finite and non-zero on the float32 row).

    python bench/dot_query_check.py OUT.json

One process, the calling thread, no HTTP. Per dim, ROUNDS rounds; each round times
both functions on the same 1 x dim float32 query (NUMBER calls each, the order
alternating between rounds) and records the per-call mean in µs. Reported per dim: the
median and minimum over rounds of each function, and the median of the per-round
differences (check minus unit).
"""
import json
import platform
import statistics
import sys
import timeit

import numpy as np

from raggio.store import _dot_check, _dot_unit

DIMS = (384, 768, 1024, 1536, 3072)
ROUNDS = 15
NUMBER = 20_000


def main(out: str) -> None:
    rng = np.random.default_rng(0)
    cases = []
    for dim in DIMS:
        # as the handler builds it: a list of Python floats into a 1 x dim float32 array
        q = np.array([rng.standard_normal(dim).tolist()], dtype=np.float32)
        fns = {"dot_unit": lambda: _dot_unit(q), "dot_check": lambda: _dot_check(q)}
        per = {name: [] for name in fns}
        for r in range(ROUNDS):
            order = list(fns) if r % 2 == 0 else list(reversed(fns))
            for name in order:
                per[name].append(timeit.timeit(fns[name], number=NUMBER) / NUMBER * 1e6)
        diffs = [c - u for c, u in zip(per["dot_check"], per["dot_unit"])]
        case = {
            "dim": dim,
            "us_per_call": per,
            "median_us": {k: statistics.median(v) for k, v in per.items()},
            "min_us": {k: min(v) for k, v in per.items()},
            "median_diff_us": statistics.median(diffs),
        }
        cases.append(case)
        m = case["median_us"]
        print(f"dim={dim:5d}  dot_unit {m['dot_unit']:6.2f} us  dot_check {m['dot_check']:6.2f} us"
              f"  diff {case['median_diff_us']:+6.2f} us")
    meta = {
        "rounds": ROUNDS, "number": NUMBER, "python": sys.version,
        "numpy": np.__version__, "machine": platform.platform(), "cpu": platform.processor(),
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "cases": cases}, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
