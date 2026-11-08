# Supporting measurements

All main serving values are medians of three independent server sessions. QPS = queries per second. CPU values aggregate all server CPU cores.

## arxiv-flat: throughput

| Case | 3.12 | 3.14 | 3.14t | 3.14t change vs 3.12 |
|---|---:|---:|---:|---:|
| vector-c1 | 54.36 | 54.53 | 53.73 | -1.2% |
| vector-c8 | 133.02 | 132.23 | 130.44 | -1.9% |
| vector-c16 | 213.10 | 216.40 | 209.78 | -1.6% |
| hybrid-c1 | 21.43 | 21.68 | 22.15 | +3.3% |
| hybrid-c8 | 37.26 | 35.55 | 39.30 | +5.5% |
| hybrid-c16 | 33.08 | 31.75 | 35.35 | +6.9% |
| filtered-c1 | 48.03 | 43.61 | 46.07 | -4.1% |

Queries/second; higher is better.

## arxiv-flat: p99 latency

| Case | 3.12 | 3.14 | 3.14t | 3.14t change vs 3.12 |
|---|---:|---:|---:|---:|
| vector-c1 | 20.39 | 20.41 | 20.60 | +1.0% |
| vector-c8 | 68.93 | 69.98 | 73.78 | +7.0% |
| vector-c16 | 92.80 | 95.26 | 97.10 | +4.6% |
| hybrid-c1 | 84.37 | 85.16 | 82.40 | -2.3% |
| hybrid-c8 | 459.74 | 449.88 | 360.98 | -21.5% |
| hybrid-c16 | 1074.81 | 1014.91 | 796.11 | -25.9% |
| filtered-c1 | 26.69 | 27.35 | 27.97 | +4.8% |

Milliseconds; lower is better.

## arxiv-flat: CPU cost

| Case | 3.12 | 3.14 | 3.14t | 3.14t change vs 3.12 |
|---|---:|---:|---:|---:|
| vector-c1 | 203.90 | 203.87 | 204.45 | +0.3% |
| vector-c8 | 113.53 | 114.97 | 115.75 | +2.0% |
| vector-c16 | 68.99 | 68.83 | 68.69 | -0.4% |
| hybrid-c1 | 238.61 | 239.95 | 237.83 | -0.3% |
| hybrid-c8 | 327.67 | 338.20 | 316.39 | -3.4% |
| hybrid-c16 | 497.56 | 510.52 | 465.37 | -6.5% |
| filtered-c1 | 33.40 | 36.23 | 33.80 | +1.2% |

Server CPU milliseconds/query; lower is better.

## arxiv-flat: repeat ranges

| Case | 3.12 QPS range | 3.14 QPS range | 3.14t QPS range |
|---|---:|---:|---:|
| vector-c1 | 50.15–54.95 | 53.90–54.80 | 52.46–53.82 |
| vector-c8 | 132.89–242.02 | 131.03–133.44 | 129.20–164.50 |
| vector-c16 | 212.05–213.75 | 214.75–219.00 | 209.26–210.93 |
| hybrid-c1 | 14.14–21.83 | 19.55–22.08 | 21.86–22.33 |
| hybrid-c8 | 37.23–37.66 | 34.07–36.66 | 38.68–40.16 |
| hybrid-c16 | 32.57–33.42 | 31.20–33.55 | 35.31–36.07 |
| filtered-c1 | 47.82–50.76 | 42.24–44.21 | 42.90–52.68 |

## arxiv-ivf: throughput

| Case | 3.12 | 3.14 | 3.14t | 3.14t change vs 3.12 |
|---|---:|---:|---:|---:|
| vector-c1 | 159.46 | 163.55 | 170.81 | +7.1% |
| vector-c8 | 387.14 | 452.69 | 510.85 | +32.0% |
| vector-c16 | 502.96 | 509.18 | 626.13 | +24.5% |
| hybrid-c1 | 23.65 | 23.16 | 23.94 | +1.2% |
| hybrid-c8 | 39.33 | 37.56 | 42.23 | +7.4% |
| hybrid-c16 | 37.11 | 34.95 | 39.75 | +7.1% |
| filtered-c1 | 165.54 | 162.20 | 161.12 | -2.7% |

Queries/second; higher is better.

## arxiv-ivf: p99 latency

| Case | 3.12 | 3.14 | 3.14t | 3.14t change vs 3.12 |
|---|---:|---:|---:|---:|
| vector-c1 | 7.96 | 7.72 | 7.73 | -2.9% |
| vector-c8 | 29.02 | 24.43 | 23.19 | -20.1% |
| vector-c16 | 53.17 | 47.87 | 43.52 | -18.2% |
| hybrid-c1 | 80.85 | 83.20 | 80.48 | -0.5% |
| hybrid-c8 | 455.96 | 463.56 | 339.21 | -25.6% |
| hybrid-c16 | 990.14 | 941.00 | 715.83 | -27.7% |
| filtered-c1 | 7.51 | 8.19 | 7.91 | +5.3% |

Milliseconds; lower is better.

## arxiv-ivf: CPU cost

| Case | 3.12 | 3.14 | 3.14t | 3.14t change vs 3.12 |
|---|---:|---:|---:|---:|
| vector-c1 | 9.66 | 11.20 | 12.52 | +29.6% |
| vector-c8 | 9.67 | 10.20 | 10.63 | +9.9% |
| vector-c16 | 9.94 | 9.52 | 10.31 | +3.7% |
| hybrid-c1 | 51.60 | 53.38 | 52.79 | +2.3% |
| hybrid-c8 | 195.82 | 206.79 | 187.58 | -4.2% |
| hybrid-c16 | 399.35 | 416.55 | 370.14 | -7.3% |
| filtered-c1 | 9.01 | 9.80 | 10.16 | +12.8% |

Server CPU milliseconds/query; lower is better.

## arxiv-ivf: repeat ranges

| Case | 3.12 QPS range | 3.14 QPS range | 3.14t QPS range |
|---|---:|---:|---:|
| vector-c1 | 87.75–168.27 | 160.51–179.18 | 151.89–178.17 |
| vector-c8 | 381.72–430.03 | 433.15–455.16 | 498.59–524.05 |
| vector-c16 | 480.45–504.74 | 491.44–590.70 | 613.33–630.24 |
| hybrid-c1 | 17.25–23.74 | 21.91–23.26 | 23.46–23.98 |
| hybrid-c8 | 37.54–39.36 | 35.40–37.74 | 42.15–42.35 |
| hybrid-c16 | 36.90–37.17 | 34.52–36.62 | 39.40–39.84 |
| filtered-c1 | 157.89–174.23 | 160.62–169.59 | 158.96–164.29 |

## Document-scoped retrieval: throughput

| Case | 3.12 | 3.14 | 3.14t | 3.14t change vs 3.12 |
|---|---:|---:|---:|---:|
| summary-c1 | 244.43 | 231.59 | 244.72 | +0.1% |
| summary-c8 | 570.38 | 516.11 | 737.52 | +29.3% |
| summary-c16 | 606.33 | 583.60 | 894.03 | +47.5% |
| doc-vector-c1 | 322.05 | 318.02 | 286.72 | -11.0% |
| doc-vector-c8 | 976.22 | 888.79 | 1432.73 | +46.8% |
| doc-vector-c16 | 1207.35 | 1250.65 | 1221.96 | +1.2% |
| doc-hybrid-c1 | 301.38 | 276.21 | 296.34 | -1.7% |
| doc-hybrid-c8 | 812.44 | 846.13 | 1454.28 | +79.0% |
| doc-hybrid-c16 | 840.45 | 660.55 | 1227.84 | +46.1% |
| doc-hybrid-cold-c1 | 253.22 | 246.56 | 243.73 | -3.7% |
| doc-hybrid-cold-c8 | 717.73 | 575.70 | 1343.64 | +87.2% |
| doc-hybrid-cold-c16 | 635.25 | 681.18 | 1139.71 | +79.4% |
| doc-k1000-c1 | 302.43 | 323.35 | 276.31 | -8.6% |
| doc-k1000-c8 | 968.61 | 981.53 | 1626.52 | +67.9% |
| doc-k1000-c16 | 1236.55 | 1339.43 | 1114.89 | -9.8% |
| doc-expand-c1 | 248.31 | 252.11 | 204.74 | -17.5% |
| doc-expand-c8 | 617.61 | 549.15 | 884.26 | +43.2% |
| doc-expand-c16 | 606.60 | 606.60 | 885.85 | +46.0% |

Queries/second; higher is better.

## Document-scoped retrieval: p99

| Case | 3.12 | 3.14 | 3.14t | 3.14t change vs 3.12 |
|---|---:|---:|---:|---:|
| summary-c1 | 4.52 | 4.56 | 4.54 | +0.5% |
| summary-c8 | 21.91 | 22.56 | 20.40 | -6.9% |
| summary-c16 | 49.38 | 44.51 | 36.08 | -26.9% |
| doc-vector-c1 | 4.15 | 4.15 | 4.66 | +12.2% |
| doc-vector-c8 | 13.74 | 16.11 | 10.66 | -22.5% |
| doc-vector-c16 | 36.21 | 27.67 | 35.97 | -0.7% |
| doc-hybrid-c1 | 4.92 | 4.76 | 5.21 | +6.0% |
| doc-hybrid-c8 | 19.99 | 14.64 | 11.22 | -43.8% |
| doc-hybrid-c16 | 38.02 | 33.31 | 39.30 | +3.4% |
| doc-hybrid-cold-c1 | 5.98 | 5.86 | 5.94 | -0.8% |
| doc-hybrid-cold-c8 | 17.79 | 19.00 | 11.24 | -36.8% |
| doc-hybrid-cold-c16 | 35.10 | 32.71 | 35.43 | +0.9% |
| doc-k1000-c1 | 4.99 | 4.65 | 5.04 | +1.1% |
| doc-k1000-c8 | 13.14 | 12.68 | 9.08 | -30.9% |
| doc-k1000-c16 | 31.68 | 22.06 | 42.35 | +33.7% |
| doc-expand-c1 | 5.46 | 5.74 | 6.38 | +17.0% |
| doc-expand-c8 | 23.96 | 27.53 | 13.07 | -45.4% |
| doc-expand-c16 | 36.95 | 35.40 | 23.91 | -35.3% |

Milliseconds; lower is better.

## Document-scoped retrieval: CPU

| Case | 3.12 | 3.14 | 3.14t | 3.14t change vs 3.12 |
|---|---:|---:|---:|---:|
| summary-c1 | 2.73 | 3.00 | 2.80 | +2.5% |
| summary-c8 | 2.51 | 2.62 | 2.33 | -7.2% |
| summary-c16 | 2.26 | 2.39 | 2.03 | -10.4% |
| doc-vector-c1 | 1.45 | 1.43 | 1.62 | +11.6% |
| doc-vector-c8 | 1.22 | 1.27 | 1.00 | -17.7% |
| doc-vector-c16 | 0.89 | 0.92 | 0.87 | -2.2% |
| doc-hybrid-c1 | 1.74 | 1.78 | 1.88 | +8.1% |
| doc-hybrid-c8 | 1.80 | 1.81 | 1.28 | -28.9% |
| doc-hybrid-c16 | 1.88 | 2.23 | 1.27 | -32.5% |
| doc-hybrid-cold-c1 | 2.23 | 2.37 | 2.19 | -1.9% |
| doc-hybrid-cold-c8 | 2.39 | 2.86 | 1.49 | -37.4% |
| doc-hybrid-cold-c16 | 2.60 | 2.57 | 1.53 | -41.3% |
| doc-k1000-c1 | 1.31 | 1.32 | 1.39 | +5.7% |
| doc-k1000-c8 | 1.14 | 1.21 | 0.87 | -23.4% |
| doc-k1000-c16 | 0.91 | 0.96 | 0.92 | +1.3% |
| doc-expand-c1 | 2.11 | 2.12 | 2.58 | +22.4% |
| doc-expand-c8 | 2.31 | 2.59 | 1.84 | -20.1% |
| doc-expand-c16 | 2.40 | 2.36 | 1.87 | -21.9% |

Server CPU milliseconds/query; lower is better.

## Ingest

| Index | 3.12 rows/s [range] | 3.14 rows/s [range] | 3.14t rows/s [range] |
|---|---:|---:|---:|
| flat | 5311 [5236–5458] | 7062 [6211–7205] | 6916 [5407–7344] |
| ivf | 3730 [3656–3763] | 3712 [3559–4026] | 3643 [3554–3896] |

## Pinned raw native kernel

All values are milliseconds per call; ten X925 cores, k=50.

| Batch queries | 3.12 | 3.14 wheel | 3.14 source | 3.14t source |
|---|---:|---:|---:|---:|
| 1 | 15.052 | 15.139 | 15.121 | 15.071 |
| 8 | 17.833 | 17.924 | 17.911 | 18.357 |
| 16 | 34.424 | 34.647 | 34.283 | 34.260 |

## control-flat: matching 100-query source-build control

| Case | 3.14 wheel QPS | 3.14 source QPS | 3.14t source QPS |
|---|---:|---:|---:|
| vector-c1 | 53.86 | 54.68 | 54.23 |
| vector-c8 | 127.48 | 127.52 | 125.05 |
| vector-c16 | 225.20 | 213.64 | 198.22 |
| hybrid-c1 | 20.46 | 20.55 | 20.49 |
| hybrid-c8 | 35.56 | 35.33 | 38.80 |
| hybrid-c16 | 34.25 | 33.77 | 35.22 |
| filtered-c1 | 45.43 | 47.48 | 45.72 |

## control-ivf: matching 100-query source-build control

| Case | 3.14 wheel QPS | 3.14 source QPS | 3.14t source QPS |
|---|---:|---:|---:|
| vector-c1 | 179.82 | 190.80 | 176.88 |
| vector-c8 | 408.19 | 456.18 | 449.50 |
| vector-c16 | 432.26 | 465.53 | 571.25 |
| hybrid-c1 | 21.44 | 21.54 | 21.73 |
| hybrid-c8 | 37.47 | 36.94 | 41.09 |
| hybrid-c16 | 36.33 | 35.68 | 38.72 |
| filtered-c1 | 159.47 | 156.25 | 164.53 |
