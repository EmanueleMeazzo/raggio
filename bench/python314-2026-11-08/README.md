# Python 3.14 evaluation evidence — 2026-11-08

Frozen application commit: `7947eba378a56225f366ff61cd716300c9305882` (raggio 0.3.0).
CPython 3.12.15, 3.14.10 and 3.14.10t; locked turbovec 1.0.0. The 314s control
builds turbovec from source on standard 3.14.10. This evidence predates any later
turbovec/dependency changes.

The idle run followed a reboot of GN100 and ran on one boot from 2026-11-07
22:53:23 to 23:34:13 UTC (8 November local completion). Shared-host measurements
were archived separately and excluded. `idle-summary.json` aggregates the numeric
files; `validation-idle.json` records completion of 109,080 measured HTTP searches,
semantic identity checks, job/data fingerprints and the known standard-3.14 test
assertion. Full pytest XML records the one expected-message assertion failure;
tests were not silently disabled or changed for this evaluation.

`provenance-*.json` records source/native-binary hashes and runtime settings.
`*-facts.txt` records boot and host facts. `runners/` retains the measurement,
aggregation and validation scripts. They expect the source, query fixture and
private data snapshots at `~/raggio-bench/python314-reevaluation-20261107/` on GN100.
The generated 500-query fixture and full corpus remain in that original archive;
the fixture generator in `arxiv-bench.py` derives it from the original arxiv corpus
and the cached exact ground truth, seed 42. Large corpora and the contaminated
run are not included in Git.

See [the evaluation](../../docs/python-3.14-reevaluation-2026-11-07.md) and
[full supporting tables](../../docs/python-3.14-reevaluation-measurements-2026-11-08.md).
