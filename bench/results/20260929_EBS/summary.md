# Benchmark summary

Mean ± stdev across reps. Warm-up and preparation uploads excluded.
The last two columns compare each variant with `minio` at the same object size and concurrency. Both read the same way: "2.00× faster" means twice the throughput, or half the p99 latency. Differences under 5% are shown as "same".

## get — GET

| object size | concurrency (clients) | variant | throughput (ops/s) | throughput (MiB/s) | p50 (ms) | p99 (ms) | p99.9 (ms) | TTFB p99 (ms) | errors (count) | throughput vs minio | p99 latency vs minio |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4KiB | 16 | minio | 2,377.8 | 9.29 | 6.27 | 17.54 | 22.96 | 17.52 | 0 | baseline | baseline |
| 4KiB | 16 | sw-minio-like | 5,072.6 | 19.81 | 3.08 | 6.70 | 10.50 | 6.66 | 0 | **2.13× faster** | **2.62× faster** |
| 4KiB | 16 | sw-tuned | 5,047.1 | 19.72 | 3.10 | 6.77 | 9.39 | 6.72 | 0 | **2.12× faster** | **2.59× faster** |
| 6MiB | 16 | minio | 294.5 | 1,766.92 | 51.73 | 105.15 | 308.68 | 60.31 | 0 | baseline | baseline |
| 6MiB | 16 | sw-minio-like | 496.5 | 2,978.75 | 30.61 | 69.67 | 92.60 | 41.10 | 0 | **1.69× faster** | **1.51× faster** |
| 6MiB | 16 | sw-tuned | 486.7 | 2,920.37 | 31.32 | 68.19 | 82.54 | 41.25 | 0 | **1.65× faster** | **1.54× faster** |
| 16MiB | 16 | minio | 126.4 | 2,021.80 | 121.41 | 228.93 | 279.65 | 108.06 | 0 | baseline | baseline |
| 16MiB | 16 | sw-minio-like | 88.4 | 1,414.53 | 171.83 | 373.05 | 421.75 | 163.91 | 0 | 1.43× slower | 1.63× slower |
| 16MiB | 16 | sw-tuned | 189.8 | 3,037.49 | 73.91 | 257.54 | 356.51 | 51.75 | 0 | **1.50× faster** | 1.12× slower |

## put — PUT

| object size | concurrency (clients) | variant | throughput (ops/s) | throughput (MiB/s) | p50 (ms) | p99 (ms) | p99.9 (ms) | TTFB p99 (ms) | errors (count) | throughput vs minio | p99 latency vs minio |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4KiB | 16 | minio | 89.3 | 0.35 | 172.66 | 295.03 | 315.12 | – | 0 | baseline | baseline |
| 4KiB | 16 | sw-minio-like | 1,028.9 | 4.02 | 15.16 | 28.79 | 36.26 | – | 0 | **11.52× faster** | **10.25× faster** |
| 4KiB | 16 | sw-tuned | 1,032.2 | 4.03 | 15.15 | 27.75 | 35.52 | – | 0 | **11.56× faster** | **10.63× faster** |
| 6MiB | 16 | minio | 10.1 | 60.38 | 1,578.11 | 1,923.73 | 1,952.17 | – | 0 | baseline | baseline |
| 6MiB | 16 | sw-minio-like | 10.7 | 64.15 | 1,358.85 | 2,206.58 | 2,378.18 | – | 0 | **1.06× faster** | 1.15× slower |
| 6MiB | 16 | sw-tuned | 10.5 | 62.99 | 1,408.32 | 2,180.27 | 2,210.37 | – | 0 | same (1.04×) | 1.13× slower |
| 16MiB | 16 | minio | 3.5 | 56.32 | 3,904.92 | 4,915.66 | 4,915.66 | – | 0 | baseline | baseline |
| 16MiB | 16 | sw-minio-like | 3.9 | 62.83 | 3,648.55 | 4,662.62 | 4,662.62 | – | 0 | **1.12× faster** | **1.05× faster** |
| 16MiB | 16 | sw-tuned | 3.9 | 62.21 | 3,565.33 | 5,025.28 | 5,025.28 | – | 0 | **1.10× faster** | same (0.98×) |

## mixed at a fixed rate — 1MiB objects, 120 req/s, 16 clients

Every variant was sent the same request rate, so compare latency and CPU, not throughput. A variant below 95% of the target rate could not keep up: it was saturated, and its latencies are not comparable with the others. Engine CPU is all engine containers together over the measured window; 100% is one vCPU.

| variant | achieved (req/s) | kept up | engine CPU (%) | errors (count) |
|---|---:|---|---:|---:|
| minio | 120.0 | yes | 42 | 0 |
| sw-minio-like | 120.0 | yes | 58 | 0 |
| sw-tuned | 120.0 | yes | 61 | 0 |

| operation | variant | p50 (ms) | p90 (ms) | p99 (ms) | p99.9 (ms) | TTFB p99 (ms) | p50 latency vs minio | p99 latency vs minio |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| GET | minio | 1.79 | 8.94 | 50.01 | 70.82 | 49.53 | baseline | baseline |
| GET | sw-minio-like | 1.74 | 4.75 | 22.87 | 98.77 | 21.73 | same (1.03×) | **2.19× faster** |
| GET | sw-tuned | 1.53 | 4.09 | 11.47 | 25.34 | 10.87 | **1.17× faster** | **4.36× faster** |
| STAT | minio | 0.72 | 7.48 | 46.52 | 67.99 | – | baseline | baseline |
| STAT | sw-minio-like | 0.63 | 1.42 | 9.14 | 75.26 | – | **1.16× faster** | **5.09× faster** |
| STAT | sw-tuned | 0.65 | 0.89 | 5.61 | 13.15 | – | **1.11× faster** | **8.30× faster** |
| PUT | minio | 29.02 | 62.21 | 122.68 | 160.69 | – | baseline | baseline |
| PUT | sw-minio-like | 19.32 | 33.88 | 80.06 | 208.15 | – | **1.50× faster** | **1.53× faster** |
| PUT | sw-tuned | 19.42 | 30.43 | 49.67 | 62.48 | – | **1.49× faster** | **2.47× faster** |
| DELETE | minio | 1.52 | 8.63 | 74.05 | 111.02 | – | baseline | baseline |
| DELETE | sw-minio-like | 0.72 | 2.03 | 12.96 | 70.83 | – | **2.13× faster** | **5.71× faster** |
| DELETE | sw-tuned | 0.74 | 1.20 | 6.10 | 12.56 | – | **2.04× faster** | **12.14× faster** |

## Storage used

From `storage.sh`: a fixed set of objects uploaded and kept, then the drives measured after `sync`, minus what the empty engine used. "Disk used ÷ data" is the real storage overhead, including filesystem block rounding; both engines are configured for 2× (EC:2 on 4 drives, replication 001), so 2.00 means no overhead beyond that. "File content ÷ data" leaves block rounding out. Inodes are files plus directories. Filesystem: ext4.

| object size | variant | objects | data stored (MiB) | disk used (MiB) | disk used ÷ data | file content ÷ data | inodes per object | disk used vs minio |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 4KiB | minio | 20,000 | 78.1 | 629.8 | 8.06 | 2.43 | 8.0 | baseline |
| 4KiB | sw-minio-like | 20,000 | 78.1 | 163.9 | 2.10 | 2.10 | 0.0 | **3.84× less** |
| 4KiB | sw-tuned | 20,000 | 78.1 | 163.9 | 2.10 | 2.10 | 0.0 | **3.84× less** |
| 128KiB | minio | 8,192 | 1,024.0 | 2,306.0 | 2.25 | 2.01 | 8.0 | baseline |
| 128KiB | sw-minio-like | 8,192 | 1,024.0 | 2,051.2 | 2.00 | 2.00 | 0.0 | **1.12× less** |
| 128KiB | sw-tuned | 8,192 | 1,024.0 | 2,051.3 | 2.00 | 2.00 | 0.0 | **1.12× less** |
| 1MiB | minio | 1,024 | 1,024.0 | 2,112.3 | 2.06 | 2.00 | 16.1 | baseline |
| 1MiB | sw-minio-like | 1,024 | 1,024.0 | 2,048.5 | 2.00 | 2.00 | 0.0 | same (1.03×) |
| 1MiB | sw-tuned | 1,024 | 1,024.0 | 2,048.6 | 2.00 | 2.00 | 0.0 | same (1.03×) |
| 6MiB | minio | 176 | 1,056.0 | 2,123.3 | 2.01 | 2.00 | 16.5 | baseline |
| 6MiB | sw-minio-like | 176 | 1,056.0 | 2,112.2 | 2.00 | 2.00 | 0.2 | same (1.01×) |
| 6MiB | sw-tuned | 176 | 1,056.0 | 2,112.3 | 2.00 | 2.00 | 0.3 | same (1.01×) |
| 16MiB | minio | 64 | 1,024.0 | 2,053.1 | 2.00 | 2.00 | 17.2 | baseline |
| 16MiB | sw-minio-like | 64 | 1,024.0 | 2,048.2 | 2.00 | 2.00 | 0.6 | same (1.00×) |
| 16MiB | sw-tuned | 64 | 1,024.0 | 2,048.3 | 2.00 | 2.00 | 0.8 | same (1.00×) |
