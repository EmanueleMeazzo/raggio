# Dot-product metric (ADR 0020): A/B of a default collection, 2026-11-25

Two questions about the branch `feat/dot-metric-float32` (source at `ed1a302`) against
`main` (`31ab17e`):

1. Does a default (cosine) collection answer the same?
2. Does a search on it cost the same?

And one about the new path: what does a `dot` collection cost next to a default one
with the same vectors?

This is a local, in-process measurement on a desktop. It does not stand in for the
DGX benchmarks behind the README's figures, and no figure there changes. Raw output:
`bench/results-dot-metric-2026-11.json`. Probe: `bench/dot_metric_ab.py`.

## Setup

- Intel Xeon E-2226G (6 cores, 3.4 GHz), 48 GB, Windows 11 (10.0.26300), CPython
  3.12.8, numpy 2.5.3, turbovec 1.1.2, SQLite 3.47.1 (Windows: SQLite's default VFS,
  no data-dir lock). Other programs were running; nothing was pinned.
- Baseline source: `git archive main src`, put first on `PYTHONPATH` (between-process
  runs) or loaded under its own package names (paired runs). Branch source: the
  checkout. Each run records which source it imported.
- Data, built once and read by every arm: 20,000 seeded float32 vectors of 1024
  dimensions with lengths from 0.5 to 2, one chunk per document, metadata
  `group = i mod 500`.
    - `plain`: a default collection, built **with the baseline source**
      (`build`). 4-bit, flat, calibrated at 10,000 rows as usual.
    - `exact`: the same vectors in a `metric: "dot"` collection, built with the
      branch (`build-dot`).
- 300 seeded queries after 30 warm-up calls. A cosine collection gets each query
  normalised (what the search route passes), the `dot` collection gets it as it is.

## 1. Same answers

Every response of every run was hashed (ids and scores of each hit, or each listed
record with its vector). Six runs per source, in the order main, branch, branch, main,
…, one process each (`run`). Per run: 1,500 direct calls for each of three cases and
300 calls for each of four `search`/listing cases on `plain`.

| Case on `plain` | Calls per run | Hashes over 12 runs |
|---|---|---|
| direct: `_search_rescored`, k=10 | 1,500 | 1 |
| direct: `_search_rescored`, k=100 | 1,500 | 1 |
| direct: `_search_allowed`, allowlist of 40, k=10 | 1,500 | 1 |
| `Collection.search`, vector, k=10 | 300 | 1 |
| `Collection.search`, vector, k=100 | 300 | 1 |
| `Collection.search`, vector, k=10, filter matching 40 records | 300 | 1 |
| `list_records`, 100 records with vectors | 300 | 1 |

The branch returns what main returns, score for score and vector for vector, from files
that main wrote. The paired runs below compared the answers call by call as well
(`same answers` in every row). An earlier series of five rounds per source (the four
`search`/listing cases only, order main, branch, main, …) gave the same four hashes.

## 2. Same cost: paired runs in one process

Between processes the p50 of one case moves by 10 to 40 % from run to run on this
machine (section 4), far more than any effect that could be expected here. So the
comparison was made inside one process (`paired`): the baseline source loaded twice
(`base_a`, `base_b`) and the branch once (`new`), each with its own `Collection` on
`plain`, every call made on all arms one after another. `base_b − base_a` is two copies
of the same code (A/A, the noise floor); `new − base_a` is the change (A/B). Per case:
1,500 calls per arm; the median of the per-call differences, its bootstrap 95 %
interval (2,000 resamples), and that median as a share of `base_a`'s p50.

Final series: four arms (the fourth is the `dot` collection, section 3), a fresh random
order of the arms for every call, four runs.

| Case | Run | p50 µs: base_a / base_b / new | A/A: base_b − base_a | A/B: new − base_a |
|---|---|---|---|---|
| scan + rescore, k=10 | 1 | 3074.6 / 3023.1 / 3022.3 | −48.70 µs [−61.45, −39.70] (−1.58 %) | −50.35 µs [−60.66, −39.35] (−1.64 %) |
| scan + rescore, k=10 | 2 | 2874.2 / 2841.3 / 2894.5 | −33.55 µs [−42.81, −21.50] (−1.17 %) | +5.60 µs [−6.30, +18.90] (+0.19 %) |
| scan + rescore, k=10 | 3 | 3180.2 / 3189.8 / 3175.0 | +9.85 µs [−11.40, +31.40] (+0.31 %) | −17.15 µs [−32.75, +2.70] (−0.54 %) |
| scan + rescore, k=10 | 4 | 3026.8 / 3003.7 / 3020.2 | −10.25 µs [−27.30, +4.30] (−0.34 %) | +8.60 µs [−6.90, +19.80] (+0.28 %) |
| scan + rescore, k=100 | 1 | 5489.3 / 5430.3 / 5436.9 | −49.55 µs [−72.75, −29.10] (−0.90 %) | −58.05 µs [−86.30, −38.10] (−1.06 %) |
| scan + rescore, k=100 | 2 | 5174.6 / 5152.2 / 5169.9 | −18.30 µs [−44.35, +13.70] (−0.35 %) | −12.20 µs [−36.30, +11.26] (−0.24 %) |
| scan + rescore, k=100 | 3 | 5954.7 / 5947.5 / 5925.2 | +8.60 µs [−33.40, +43.80] (+0.14 %) | +1.85 µs [−53.15, +47.43] (+0.03 %) |
| scan + rescore, k=100 | 4 | 5560.1 / 5553.8 / 5561.3 | −3.00 µs [−22.40, +16.80] (−0.05 %) | +7.75 µs [−15.15, +24.60] (+0.14 %) |
| allowlist of 40, k=10 | 1 | 306.9 / 308.1 / 306.3 | +0.45 µs [−0.80, +1.70] (+0.15 %) | −0.85 µs [−1.95, +0.10] (−0.28 %) |
| allowlist of 40, k=10 | 2 | 308.6 / 308.0 / 309.7 | −0.70 µs [−1.90, +0.60] (−0.23 %) | −0.10 µs [−1.35, +1.00] (−0.03 %) |
| allowlist of 40, k=10 | 3 | 306.9 / 306.2 / 305.1 | −1.40 µs [−2.50, −0.10] (−0.46 %) | −1.40 µs [−2.50, −0.50] (−0.46 %) |
| allowlist of 40, k=10 | 4 | 306.1 / 305.2 / 304.5 | −1.30 µs [−2.45, −0.25] (−0.42 %) | −0.70 µs [−1.70, +0.20] (−0.23 %) |

Reading: two copies of main differ by −1.58 % to +0.31 % of the p50; the branch differs
from main by −1.64 % to +0.28 %. The change is not distinguishable from running the
same code twice, in any case or run. The instrument resolves about 1.5 % here and no
finer; what the change adds to a default search by construction is a handful of
attribute reads and branches per call, well under a microsecond on calls of 300 to
5,500 µs.

Earlier series, kept because they were run. Both used a fixed rotation of the arms,
which always runs the same arm after the same other arm:

Three arms (no `dot` arm), three runs:

| Case | Run | p50 µs: base_a / base_b / new | A/A: base_b − base_a | A/B: new − base_a |
|---|---|---|---|---|
| scan + rescore, k=10 | 1 | 2452.8 / 2454.7 / 2453.6 | −2.65 µs [−8.11, +2.10] (−0.11 %) | +4.80 µs [+1.10, +10.25] (+0.20 %) |
| scan + rescore, k=10 | 2 | 2529.0 / 2521.2 / 2540.1 | −14.50 µs [−22.15, −8.80] (−0.57 %) | +9.45 µs [+4.09, +16.55] (+0.37 %) |
| scan + rescore, k=10 | 3 | 2517.0 / 2511.6 / 2517.9 | −9.20 µs [−12.65, −5.40] (−0.37 %) | +3.80 µs [−1.70, +9.20] (+0.15 %) |
| scan + rescore, k=100 | 1 | 4503.7 / 4506.9 / 4489.5 | −13.10 µs [−26.50, +2.95] (−0.29 %) | +3.15 µs [−7.00, +13.20] (+0.07 %) |
| scan + rescore, k=100 | 2 | 4572.5 / 4565.6 / 4587.6 | −0.75 µs [−17.20, +17.40] (−0.02 %) | +8.15 µs [−6.05, +24.30] (+0.18 %) |
| scan + rescore, k=100 | 3 | 5037.0 / 5012.1 / 5013.0 | −25.75 µs [−43.65, −12.80] (−0.51 %) | −9.45 µs [−27.40, +2.60] (−0.19 %) |
| allowlist of 40, k=10 | 1 | 224.4 / 226.4 / 224.9 | +0.50 µs [+0.15, +0.80] (+0.22 %) | +0.50 µs [+0.20, +0.85] (+0.22 %) |
| allowlist of 40, k=10 | 2 | 237.6 / 238.1 / 238.1 | −0.10 µs [−0.40, +0.20] (−0.04 %) | +0.30 µs [+0.00, +0.60] (+0.13 %) |
| allowlist of 40, k=10 | 3 | 261.5 / 260.5 / 261.3 | −1.20 µs [−1.75, −0.70] (−0.46 %) | −0.25 µs [−0.60, +0.30] (−0.10 %) |

A/A from −0.57 % to +0.22 %, A/B from −0.19 % to +0.37 %: the same picture. In the
k=10 case the branch's three medians are all above zero (+4 to +9 µs on 2.5 ms) while
the A/A's are all below; the random-order series above does not repeat that (−50, +6,
−17, +9 µs), so it is read as an effect of the fixed order, not of the code.

Four arms with the fixed rotation, three runs. Here `base_a` always ran right after the
`dot` arm, whose larger reads leave colder caches, so both other cosine arms come out
"faster" than `base_a` by a similar amount (A/A −0.64 to −2.55 %, A/B −3.07 to
+0.37 %). This is why the order was randomised; the rows are in the JSON
(`paired_four_arms_fixed_rotation`).

## 3. What the dot path costs

The fourth arm of the final series: the branch on `exact`, the same 20,000 vectors under
`metric: "dot"`, against `base_a` on `plain`. Not the same computation, so this is a
price, not a regression.

| Case | Run | p50 µs: default (base_a) / dot | dot − default |
|---|---|---|---|
| scan + rescore, k=10 (50 candidates) | 1 | 3074.6 / 3197.9 | +107.00 µs [+98.90, +118.60] (+3.48 %) |
| scan + rescore, k=10 | 2 | 2874.2 / 3042.1 | +161.25 µs [+151.00, +171.60] (+5.61 %) |
| scan + rescore, k=10 | 3 | 3180.2 / 3356.6 | +164.60 µs [+140.15, +182.20] (+5.18 %) |
| scan + rescore, k=10 | 4 | 3026.8 / 3147.7 | +122.80 µs [+109.70, +135.50] (+4.06 %) |
| scan + rescore, k=100 (200 candidates) | 1 | 5489.3 / 6289.0 | +773.05 µs [+745.60, +796.00] (+14.08 %) |
| scan + rescore, k=100 | 2 | 5174.6 / 5902.2 | +693.40 µs [+665.50, +717.95] (+13.40 %) |
| scan + rescore, k=100 | 3 | 5954.7 / 7012.1 | +1012.85 µs [+965.70, +1054.95] (+17.01 %) |
| scan + rescore, k=100 | 4 | 5560.1 / 6408.1 | +829.20 µs [+813.85, +851.00] (+14.91 %) |
| allowlist of 40, k=10 (no scan) | 1 | 306.9 / 468.4 | +156.00 µs [+153.20, +158.20] (+50.83 %) |
| allowlist of 40, k=10 | 2 | 308.6 / 474.8 | +160.75 µs [+158.60, +163.25] (+52.08 %) |
| allowlist of 40, k=10 | 3 | 306.9 / 483.5 | +171.95 µs [+168.50, +175.30] (+56.02 %) |
| allowlist of 40, k=10 | 4 | 306.1 / 474.4 | +160.70 µs [+158.40, +163.80] (+52.50 %) |

About 2 to 5 µs more per candidate at 1024 dimensions: twice the bytes per vector, read
from two pages instead of one (below), a conversion to float64, and a unit copy of the
query. On this small collection the scan is cheap, so the share is large; on a large
one the scan dominates and the same microseconds are a smaller share. Not measured at
scale.

## Disk

| | `plain` (default) | `exact` (dot) |
|---|---|---|
| `meta.db` | 84,082,688 bytes, 20,528 pages | 94,171,136 bytes, 22,991 pages |
| `index.tvim` | 11,614,548 bytes | 11,614,548 bytes |
| `vecs` payload | 20,000 × 2,048 bytes | 20,000 × 4,096 bytes |

Twice the payload, 12 % more file. With 4 KiB pages an fp16 row of 2,048 bytes has a
leaf page to itself (two do not fit), so the default collection already spends about
4.1 KiB per vector at this dimension; a float32 row of 4,096 bytes does not fit a leaf
cell and goes to one overflow page plus a share of a leaf (the page counts fit one
overflow page per row and about eight rows' cells per leaf; `dbstat` is not compiled
into this SQLite, so this is from the counts, not from a page listing). Other
dimensions pack differently and were not measured.

## 4. Between-process timings, for the record

The six runs per source of section 1 also timed each case (p50 in µs per run, then the
median of the six).

| Case | main | branch | median main / branch |
|---|---|---|---|
| plain direct: scan + rescore, k=10 | 2833, 2984, 2872, 2424, 2453, 2447 | 2979, 2502, 2733, 2837, 2498, 2458 | 2643 / 2617 |
| plain direct: scan + rescore, k=100 | 5460, 5555, 5524, 5078, 4969, 4954 | 5493, 4802, 5351, 5452, 4625, 4641 | 5269 / 5076 |
| plain direct: allowlist of 40, k=10 | 275, 282, 232, 222, 233, 230 | 236, 234, 248, 249, 241, 220 | 232 / 239 |
| plain search: vector k=10 | 3863, 3882, 3153, 2727, 2751, 2800 | 2901, 2908, 3422, 3084, 2856, 2814 | 2976 / 2904 |
| plain search: vector k=100 | 6452, 6002, 6468, 5517, 5907, 5482 | 5935, 5417, 6493, 5704, 4863, 5514 | 5955 / 5609 |
| plain search: vector k=10, filter of 40 | 644, 638, 515, 522, 491, 503 | 537, 549, 648, 576, 485, 472 | 519 / 543 |
| plain list 100 with vectors | 6205, 5849, 5814, 5115, 5112, 5086 | 5714, 5296, 5782, 5667, 5039, 4959 | 5465 / 5482 |
| exact direct: scan + rescore, k=10 | – | 2885, 2967, 2898, 3280, 2513, 2486 | – / 2892 |
| exact direct: scan + rescore, k=100 | – | 5670, 6532, 6138, 5405, 5094, 4961 | – / 5538 |
| exact direct: allowlist of 40, k=10 | – | 337, 360, 343, 329, 338, 343 | – / 341 |
| exact search: vector k=10 | – | 3193, 3334, 3087, 2896, 2984, 3109 | – / 3098 |
| exact search: vector k=100 | – | 5921, 6505, 7453, 5820, 5694, 5742 | – / 5870 |

The medians of the two sources differ by −6 % to +5 % with no common sign, inside each
source's own spread from run to run. Nothing can be read from these beyond "no large
difference"; section 2 is the comparison.

The earlier five-round series (order main, branch, main, …; its JSON was overwritten by
the six-round series, the figures are from its printed summary): plain vector k=10,
main 2808, 2995, 2902, 2712, 3024 and branch 2948, 2913, 2782, 3144, 3481; k=100, main
5538, 5632, 5462, 5760, 6217 and branch 5419, 5314, 5833, 6046, 6613; filter of 40,
main 525, 539, 489, 530, 744 and branch 518, 497, 539, 539, 630; list 100 with vectors,
main 5151, 5131, 5187, 5395, 6016 and branch 5086, 5063, 5134, 5543, 5844.

## Limits

- One machine, Windows, 20,000 vectors, one dimension, a flat index, one client. No
  HTTP, no concurrency, no IVF, no hybrid or text case, no memory measurement (the
  change adds two class attributes and one string per collection config).
- The paired design measures the functions the option touches, on the calling thread.
  It resolves about 1.5 % of a call; a smaller difference would not show.
- The files were warm in the page cache throughout.
- Recall of the dot-product ranking on a real corpus was not measured (ADR 0020, "The
  guarantee").
