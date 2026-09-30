# Benchmark summary

Mean ± stdev across reps. Warm-up and preparation uploads excluded.
The last two columns compare each variant with `minio` at the same object size and concurrency. Both read the same way: "2.00× faster" means twice the throughput, or half the p99 latency. Differences under 5% are shown as "same".

## get — GET

| object size | concurrency (clients) | variant | throughput (ops/s) | throughput (MiB/s) | p50 (ms) | p99 (ms) | p99.9 (ms) | TTFB p99 (ms) | errors (count) | throughput vs minio | p99 latency vs minio |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4KiB | 16 | minio | 2,273.4 | 8.88 | 6.58 | 18.10 | 22.68 | 18.09 | 0 | baseline | baseline |
| 4KiB | 16 | sw-minio-like | 4,844.5 | 18.92 | 3.23 | 7.02 | 10.00 | 7.00 | 0 | **2.13× faster** | **2.58× faster** |
| 4KiB | 16 | sw-tuned | 4,814.8 | 18.81 | 3.25 | 7.11 | 10.74 | 7.06 | 0 | **2.12× faster** | **2.55× faster** |
| 6MiB | 16 | minio | 273.3 | 1,639.81 | 56.55 | 112.28 | 132.26 | 64.19 | 0 | baseline | baseline |
| 6MiB | 16 | sw-minio-like | 474.9 | 2,849.44 | 32.30 | 71.12 | 88.36 | 43.15 | 0 | **1.74× faster** | **1.58× faster** |
| 6MiB | 16 | sw-tuned | 451.3 | 2,707.77 | 33.69 | 75.37 | 92.93 | 40.66 | 0 | **1.65× faster** | **1.49× faster** |
| 16MiB | 16 | minio | 115.5 | 1,848.25 | 131.00 | 261.16 | 305.06 | 116.12 | 0 | baseline | baseline |
| 16MiB | 16 | sw-minio-like | 82.5 | 1,320.76 | 182.28 | 415.28 | 548.45 | 167.48 | 0 | 1.40× slower | 1.59× slower |
| 16MiB | 16 | sw-tuned | 172.8 | 2,765.48 | 87.60 | 208.55 | 245.66 | 34.95 | 0 | **1.50× faster** | **1.25× faster** |

## put — PUT

| object size | concurrency (clients) | variant | throughput (ops/s) | throughput (MiB/s) | p50 (ms) | p99 (ms) | p99.9 (ms) | TTFB p99 (ms) | errors (count) | throughput vs minio | p99 latency vs minio |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4KiB | 16 | minio | 728.4 | 2.85 | 21.10 | 44.44 | 135.36 | – | 0 | baseline | baseline |
| 4KiB | 16 | sw-minio-like | 946.1 | 3.70 | 16.55 | 32.08 | 42.30 | – | 0 | **1.30× faster** | **1.39× faster** |
| 4KiB | 16 | sw-tuned | 947.3 | 3.70 | 16.45 | 30.13 | 39.90 | – | 0 | **1.30× faster** | **1.47× faster** |
| 6MiB | 16 | minio | 10.1 | 60.67 | 1,519.25 | 1,821.89 | 1,909.85 | – | 0 | baseline | baseline |
| 6MiB | 16 | sw-minio-like | 10.3 | 61.84 | 1,407.99 | 2,000.51 | 2,065.66 | – | 0 | same (1.02×) | 1.10× slower |
| 6MiB | 16 | sw-tuned | 10.2 | 61.18 | 1,429.27 | 2,241.75 | 2,311.47 | – | 0 | same (1.01×) | 1.23× slower |
| 16MiB | 16 | minio | 3.9 | 62.15 | 3,953.22 | 4,606.72 | 4,606.72 | – | 0 | baseline | baseline |
| 16MiB | 16 | sw-minio-like | 3.8 | 61.44 | 3,611.09 | 4,854.30 | 4,854.30 | – | 0 | same (0.99×) | 1.05× slower |
| 16MiB | 16 | sw-tuned | 3.9 | 62.75 | 3,620.13 | 5,261.84 | 5,261.84 | – | 0 | same (1.01×) | 1.14× slower |

## mixed at a fixed rate — 1MiB objects, 120 req/s, 16 clients

Every variant was sent the same request rate, so compare latency and CPU, not throughput. A variant below 95% of the target rate could not keep up: it was saturated, and its latencies are not comparable with the others. Engine CPU is all engine containers together over the measured window; 100% is one vCPU.

| variant | achieved (req/s) | kept up | engine CPU (%) | errors (count) |
|---|---:|---|---:|---:|
| minio | 120.0 | yes | 42 | 0 |
| sw-minio-like | 120.0 | yes | 65 | 0 |
| sw-tuned | 120.0 | yes | 68 | 0 |

| operation | variant | p50 (ms) | p90 (ms) | p99 (ms) | p99.9 (ms) | TTFB p99 (ms) | p50 latency vs minio | p99 latency vs minio |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| GET | minio | 2.08 | 3.19 | 5.70 | 9.82 | 5.04 | baseline | baseline |
| GET | sw-minio-like | 1.83 | 3.85 | 12.97 | 24.03 | 11.72 | **1.14× faster** | 2.27× slower |
| GET | sw-tuned | 1.90 | 4.55 | 11.97 | 19.97 | 10.72 | **1.09× faster** | 2.10× slower |
| STAT | minio | 0.93 | 1.72 | 3.15 | 8.79 | – | baseline | baseline |
| STAT | sw-minio-like | 0.87 | 1.18 | 5.19 | 8.17 | – | **1.07× faster** | 1.64× slower |
| STAT | sw-tuned | 0.86 | 1.18 | 5.88 | 11.05 | – | **1.08× faster** | 1.86× slower |
| PUT | minio | 11.42 | 12.41 | 32.06 | 58.14 | – | baseline | baseline |
| PUT | sw-minio-like | 20.06 | 30.65 | 55.28 | 61.88 | – | 1.76× slower | 1.72× slower |
| PUT | sw-tuned | 20.10 | 31.71 | 53.41 | 66.27 | – | 1.76× slower | 1.67× slower |
| DELETE | minio | 1.79 | 2.51 | 4.35 | 8.51 | – | baseline | baseline |
| DELETE | sw-minio-like | 0.99 | 1.56 | 6.99 | 13.34 | – | **1.80× faster** | 1.60× slower |
| DELETE | sw-tuned | 0.98 | 1.35 | 6.96 | 20.04 | – | **1.83× faster** | 1.60× slower |

## Storage used

From `storage.sh`: a fixed set of objects uploaded and kept, then the drives measured after `sync`, minus what the empty engine used. "Disk used ÷ data" is the real storage overhead, including filesystem block rounding; both engines are configured for 2× (EC:2 on 4 drives, replication 001), so 2.00 means no overhead beyond that. "File content ÷ data" leaves block rounding out. Inodes are files plus directories. Filesystem: xfs.

| object size | variant | objects | data stored (MiB) | disk used (MiB) | disk used ÷ data | file content ÷ data | inodes per object | disk used vs minio |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 4KiB | minio | 20,000 | 78.1 | 317.5 | 4.06 | 2.43 | 8.0 | baseline |
| 4KiB | sw-minio-like | 20,000 | 78.1 | 199.3 | 2.55 | 2.10 | 0.0 | **1.59× less** |
| 4KiB | sw-tuned | 20,000 | 78.1 | 262.6 | 3.36 | 2.10 | 0.0 | **1.21× less** |
| 128KiB | minio | 8,192 | 1,024.0 | 2,178.5 | 2.13 | 2.01 | 8.0 | baseline |
| 128KiB | sw-minio-like | 8,192 | 1,024.0 | 3,069.9 | 3.00 | 2.00 | 0.0 | 1.41× more |
| 128KiB | sw-tuned | 8,192 | 1,024.0 | 3,067.5 | 3.00 | 2.00 | 0.0 | 1.41× more |
| 1MiB | minio | 1,024 | 1,024.0 | 2,080.3 | 2.03 | 2.00 | 16.1 | baseline |
| 1MiB | sw-minio-like | 1,024 | 1,024.0 | 3,024.4 | 2.95 | 2.00 | 0.0 | 1.45× more |
| 1MiB | sw-tuned | 1,024 | 1,024.0 | 3,264.5 | 3.19 | 2.00 | 0.0 | 1.57× more |
| 6MiB | minio | 176 | 1,056.0 | 2,117.8 | 2.01 | 2.00 | 16.5 | baseline |
| 6MiB | sw-minio-like | 176 | 1,056.0 | 2,760.1 | 2.61 | 2.00 | 0.2 | 1.30× more |
| 6MiB | sw-tuned | 176 | 1,056.0 | 3,420.2 | 3.24 | 2.00 | 0.3 | 1.61× more |
| 16MiB | minio | 64 | 1,024.0 | 2,050.0 | 2.00 | 2.00 | 17.2 | baseline |
| 16MiB | sw-minio-like | 64 | 1,024.0 | 2,880.1 | 2.81 | 2.00 | 0.6 | 1.40× more |
| 16MiB | sw-tuned | 64 | 1,024.0 | 3,072.2 | 3.00 | 2.00 | 0.8 | 1.50× more |
