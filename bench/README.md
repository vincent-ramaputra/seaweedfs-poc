# SeaweedFS vs MinIO benchmark

Performance comparison of SeaweedFS against MinIO in the topology production
uses today: **a single node with multiple drives**. It measures speed only,
not features, failure handling or operations.

The load comes from [warp](https://github.com/minio/warp), an S3 load
generator. It pretends to be many S3 clients at once, sends real S3 requests,
and records how long each request takes. Both engines get the exact same warp
commands.

> [!WARNING]
> Every test **wipes `DRIVE1`–`DRIVE4` completely**. Point them only at
> drives or directories dedicated to this benchmark.

## Contents

- [SeaweedFS vs MinIO benchmark](#seaweedfs-vs-minio-benchmark)
  - [Contents](#contents)
  - [What is compared](#what-is-compared)
  - [How the comparison is kept fair](#how-the-comparison-is-kept-fair)
  - [What is tested](#what-is-tested)
  - [What is measured](#what-is-measured)
  - [Requirements](#requirements)
  - [Quick run](#quick-run)
  - [Full benchmark](#full-benchmark)
    - [Duration and disk space](#duration-and-disk-space)
  - [Storage test](#storage-test)
  - [Configuration](#configuration)
    - [`bench/.env`](#benchenv)
    - [Matrix files](#matrix-files)
  - [Output](#output)
  - [Reading the summary](#reading-the-summary)
  - [Troubleshooting](#troubleshooting)
  - [Caveats](#caveats)
  - [Files](#files)

## What is compared

| Variant | Setup | Question it answers |
|---|---|---|
| `minio` | MinIO, 4 drives, erasure coding `EC:2` | Baseline: production today |
| `sw-minio-like` | SeaweedFS configured to match MinIO: one volume server per drive, every object stored twice (replication `001`, same 2x overhead), `fs.configure -fsync` set, otherwise SeaweedFS defaults. **S3 writes are still not synced to disk** (see [below](#how-the-comparison-is-kept-fair)) | Is SeaweedFS faster with the same layout as MinIO? Like-for-like for reads only |
| `sw-tuned` | SeaweedFS configured for speed, including options MinIO has no equivalent for (see below). Same 2x overhead. | How fast can SeaweedFS get? |

`sw-tuned` differs from `sw-minio-like` in:

| Setting | `sw-minio-like` | `sw-tuned` | Why |
|---|---|---|---|
| Disk syncing (`fs.configure -fsync`) | set | not set (SeaweedFS default) | Meant to make SeaweedFS sync each write before replying. In 4.40 it has no effect on S3 PUTs, so in practice both variants reply before the OS flushes the write to disk. |
| Volumes grown at once (`fs.configure -volumeGrowthCount`) | default | `SW_TUNED_GROWTH_COUNT` (8 physical = 4 replicated pairs) | Creates more writable volumes up front instead of stalling writes while growing a few at a time. |
| S3 GET cache (`-s3.cacheCapacityMB`) | off | `SW_TUNED_CACHE_MB` (1024) | The S3 gateway serves repeated GETs of small objects from memory, skipping the volume server. Large (8 MiB) chunks are not cached. |

Tried and left out: `-saveToFilerLimit` (store small objects in the filer
store) and `-maxMB` (chunk size) have no effect on S3 uploads in SeaweedFS
4.40. The S3 gateway always writes to volume servers in 8 MiB chunks.

## How the comparison is kept fair

- **Same drives, one engine at a time.** Both engines use the same four drive
  paths. Before every test, both engines are stopped, the drives are wiped,
  and the engine under test starts empty.
- **Same storage overhead.** MinIO's `EC:2` on 4 drives stores 2 data + 2
  parity pieces, so 2x the raw data. SeaweedFS writes always go to replicated
  volumes; its erasure coding is meant for warm storage and only converts
  full, idle volumes later in the background. So SeaweedFS uses replication
  `001` (2 copies, also 2x). Note the difference in fault tolerance: `EC:2`
  survives losing 2 drives, replication `001` survives losing 1.
- **Not the same write durability (known gap).** MinIO syncs each write,
  including its metadata (`xl.meta`), to disk before replying. SeaweedFS 4.40
  does not sync S3 PUTs in either variant: `sw-minio-like` sets
  `fs.configure -locationPrefix=/buckets/ -fsync=true`, and `fs.configure`
  shows it stored, but under a 4KiB PUT load a volume server made ~15,000
  `pwrite64` calls and zero `fsync`/`fdatasync` in 10 s (strace, EC2
  m5d.xlarge, 2026-09-24). The filer's metadata store (LevelDB) isn't synced
  either. A power loss or host crash can lose recently acknowledged SeaweedFS
  writes, and SeaweedFS PUT results are an upper bound, not a like-for-like
  number. To check a new version or setting, trace a volume server during a
  PUT run:

  ```bash
  PID=$(docker inspect -f '{{.State.Pid}}' bench-seaweedfs-volume1-1)
  sudo timeout -s INT 10 strace -f -c -e trace=pwrite64,fsync,fdatasync -p "$PID"
  ```
- **Same CPUs.** Every engine container is pinned to `BENCH_CPUSET`, and warp
  to `WARP_CPUSET`. The two must not overlap: an unpinned load generator lands
  on the engine's cores and makes both engines look equally slow.
- **Same network path.** All containers use host networking, and SeaweedFS
  runs its S3 API inside the filer process, removing a network hop.
- **Same authentication cost.** SeaweedFS is given an S3 user, so it
  verifies request signatures like MinIO does. Without one it would skip
  authentication entirely.
- **Warm-up and setup excluded.** The first `SKIP` seconds of each test and
  warp's setup uploads don't count toward the results.
- **Page cache dropped** between tests, if passwordless `sudo` is available.
  Otherwise, reads may hit data still cached in memory, and a warning is printed.

## What is tested

Each **test cell** is one warp run against a freshly started engine.

| Test | What warp does | What it tells you | Repeated over |
|---|---|---|---|
| **PUT** | Uploads new objects, each with a new name, as fast as possible (single-part) | Write speed | object size × concurrency |
| **GET** | Uploads a set of objects, then downloads them repeatedly | Read speed and time to first byte | object size × concurrency |
| **Mixed** | GET, HEAD, PUT and DELETE at the same time, in a set ratio, at a fixed total request rate (`MIXED_RPS_LIMIT`) | Latency and CPU at a realistic load, rather than maximum throughput | concurrency |
| **Long mixed** | Mixed workload for a long period (full matrix only) | Latency drift and background work (SeaweedFS vacuum, MinIO scanner) | once, only if `LONG_MIXED_DURATION` is set |
| **Storage** (`storage.sh`, separate from the sweep) | Uploads a fixed set of objects, keeps them, and measures the drives | Disk space and inodes used per byte stored | object size |

**Concurrency** is the number of requests in flight at once. Increasing it
shows where each engine stops getting faster and latency starts climbing.

GET reads the same small set of objects many times, so those objects are
usually in the OS page cache. GET results therefore mostly reflect the
engine's request handling plus memory speed, unless the set of objects is
larger than RAM.

## What is measured

Per test cell and per operation:

| Metric | Meaning |
|---|---|
| **ops/s** | Successful requests per second |
| **MiB/s** | Data transferred per second |
| **p50 / p99 / p99.9** | Latency (ms) that 50% / 99% / 99.9% of requests beat. The tail (p99, p99.9) is what users notice. |
| **TTFB p99** | Time until the first byte of a GET response arrives |
| **errors** | Failed requests |
| **vs minio** | Throughput and p99 compared with MinIO for the same cell, both as "N× faster" or "N× slower" (for p99, "2× faster" means half the latency). Differences under 5% show as "same". |

Percentiles are computed exactly from every recorded request (warp `--full`),
not approximated from warp's per-segment summaries.

Each cell also records container CPU/memory (`docker stats`, including warp
itself as `bench-warp`) and disk load (`iostat`), to tell whether the engine,
the disk or the load generator was the bottleneck.

## Requirements

- Docker with Compose v2
- `curl`, `jq`, `zstd`, `iostat` (package `sysstat`) and Python 3.10+ on the host
- Free host ports (all engines use host networking):

  | Engine | Ports |
  |---|---|
  | MinIO | 9100 (S3), 9101 (console) |
  | SeaweedFS | 9433, 19433, 9434 (master) · 8481–8484, 18481–18484, 9481–9484 (volume servers) · 8988, 18988, 9489 (filer) · 8433, 18433, 9490 (S3) |

- Optional: passwordless `sudo`, to drop the page cache between tests

Images used: `quay.io/minio/minio`, `chrislusf/seaweedfs`, `minio/warp`, set in
`.env`. (MinIO's images moved off Docker Hub in 2025.)

## Quick run

The default matrix (`matrix.env`) gives a first impression in **about 15
minutes for all three variants**. It runs PUT and GET on small (4 KiB) and
large (16 MiB) objects plus one mixed workload, at a single concurrency
level (16), 15 seconds per test, once. It also checks that the harness works.

It is too short and too narrow to base a decision on: there is no
concurrency curve, no repetitions to show variance, and each test measures
only ~12 seconds.

On a laptop, `matrix.small.env` is the smallest run worth quoting. It drops the
concurrency curve but keeps `REPS=3`, so every difference arrives with a stdev
next to it:

```bash
bench/sweep.sh <variant> bench/matrix.small.env
```

Use the [full benchmark](#full-benchmark) on a production-shaped machine for
decisions.

```bash
cp bench/.env.example bench/.env        # defaults work on a dev machine

export BENCH_RUN=quick-$(date +%Y%m%d-%H%M)
for v in minio sw-minio-like sw-tuned; do
  bench/sweep.sh "$v"
done

python3 bench/summarize.py "bench/results/$BENCH_RUN"
```

Then open `bench/results/$BENCH_RUN/summary.md`.

## Full benchmark

Run this on a machine shaped like production, not a laptop.

1. **Match production.** Check the production MinIO drive count and parity:
   ```bash
   mc admin info <prod-alias>
   ```
   In `bench/.env`, set `MINIO_IMAGE` to the production release and
   `MINIO_PARITY` to its parity. Point `DRIVE1`–`DRIVE4` at four separate
   physical drives. The compose files are written for 4 drives; see
   [Caveats](#caveats) for other counts.

   On an EC2 `m5d.xlarge` (4 vCPUs, 16 GiB RAM, one 150 GB NVMe instance
   store — no 4-drive layout available), copy `bench/.env.m5d-xlarge`
   instead of `.env.example`; it has this single-disk caveat and the CPU
   split for that instance's 2 physical cores already worked out.
2. **Isolate the load generator.** Set `BENCH_CPUSET` to cores used by the
   engines only, and keep warp and other workloads off them. Ideally run warp
   from a separate machine.
3. **Set the production traffic mix.** In `bench/matrix.full.env`, set
   `MIXED_GET`, `MIXED_STAT`, `MIXED_PUT`, `MIXED_DELETE` to production's
   request ratio, `MIXED_RPS_LIMIT` to production's request rate (the
   per-second rate of MinIO's `minio_s3_requests_total` metric at a busy
   hour), and adjust `SIZES` to production's object sizes.
4. **Run every variant** under the same run name, inside `tmux` or `screen`.
   It takes ~1½ hours for all three (see
   [Duration and disk space](#duration-and-disk-space)).
   ```bash
   export BENCH_RUN=$(date +%Y%m%d)
   for v in minio sw-minio-like sw-tuned; do
     bench/sweep.sh "$v" bench/matrix.full.env
   done
   ```
5. **Summarize:**
   ```bash
   python3 bench/summarize.py "bench/results/$BENCH_RUN"
   ```

### Duration and disk space

Estimates. The default matrix is sized for a laptop; the full matrix for a
production-like server.

| Matrix | Cells per variant | Per variant | All three |
|---|---|---|---|
| `matrix.env` (default) | 6 × 15 s + 1 × 1 min | ~5 min | **~15 min** |
| `matrix.small.env` (laptop) | 18 × 20 s + 3 × 1 min | ~20 min | **~1 h** |
| `matrix.full.env` | 25 × 1 min | ~45 min | **~2¼ h** |

Besides the measured time, each cell spends time restarting the engine
(~10 s), uploading setup data and cleaning up; that overhead is included
above.

Time scales with the number of cells: each size, concurrency level or rep
you add multiplies it. For example, `REPS=2` doubles the full matrix, and
`LONG_MIXED_DURATION=10m` adds ~35 minutes for all three variants. Large
objects are also slow to set up: each GET cell first uploads
`GET_MAX_BYTES` of data, which takes ~45 minutes for 200 GiB at 75 MiB/s.

**Disk space.** The drives need room for `GET_MAX_BYTES` × the storage
overhead (2x for both `EC:2` and replication `001`) plus headroom:

| Matrix | `GET_MAX_BYTES` | Free space needed across the drives |
|---|---|---|
| `matrix.env` | 512 MiB | ~2 GB |
| `matrix.small.env` | 256 MiB | ~1 GB, plus up to ~10 GB for a 20-second PUT of 8 MiB objects |
| `matrix.full.env` | 1 GiB | ~3 GB, plus up to ~30 GB for a 1-minute PUT of 12 MiB objects on fast drives |

The drives default to directories under `bench/`, so on a dev machine this
space comes out of that filesystem. Run `matrix.full.env` on the benchmark
server, not a laptop.

**Resuming.** A sweep skips cells that already have results, so after an
interruption, rerun the same command with the same `BENCH_RUN`. To redo a
cell, delete its files first (`<cell>.*`).

**Stopping the engines.** A sweep leaves the last engine running for
inspection. Stop both with:

```bash
docker compose -f bench/minio-compose.yml down
docker compose -f bench/seaweedfs-compose.yml down
```

## Storage test

`storage.sh` measures storage efficiency: how much disk space and how many
inodes each engine uses for the same data. It is separate from the sweep
because it measures space, not speed, and needs the objects to stay on disk
(warp deletes them at the end of every sweep cell).

For each size in `STORAGE_SIZES` it resets the engine, measures the empty
drives, uploads a fixed set of objects (single-part, like the PUT cells) with
`warp get --noclear`, runs `sync`, and measures the drives again. The
difference is what the objects cost. Both engines are configured to store
data twice (`EC:2` on 4 drives, replication `001`), so **2.00× the data is
the expected minimum**; anything above is overhead from how each engine lays
objects out on disk.

```bash
export BENCH_RUN=$(date +%Y%m%d)
for v in minio sw-tuned; do    # sw-minio-like stores data the same way as sw-tuned
  bench/storage.sh "$v"
done
python3 bench/summarize.py "bench/results/$BENCH_RUN"
```

It takes a few minutes per variant: the uploads are ~20,000 small objects or
up to 1 GiB per size. The results go into a "Storage used" table in
`summary.md`, so it can share a `BENCH_RUN` with a sweep or have its own.

| Variable | Default | Meaning |
|---|---|---|
| `STORAGE_SIZES` | 4KiB 128KiB 1MiB 6MiB 16MiB | Object sizes to measure |
| `STORAGE_OBJECTS` | 20000 | Objects per size, capped by `STORAGE_MAX_BYTES` |
| `STORAGE_MAX_BYTES` | 1 GiB | Caps objects × size, so large sizes upload quickly |
| `STORAGE_CONCURRENCY` | 16 | Parallel uploads |

What to expect: small objects are where the engines differ. MinIO keeps a
directory and an `xl.meta` file per object on every drive, and each file takes
at least one filesystem block, so a 4 KiB object costs several times its size
and ~8 inodes. SeaweedFS appends objects to large volume files, so it stays
close to 2× with almost no inodes per object. For multi-MiB objects the
per-object overhead is negligible and both engines land at ~2×.

Limits:

- **Filesystem matters.** Block size and how directories are stored (ext4
  gives every directory a 4 KiB block; XFS stores small ones in the inode)
  change MinIO's small-object overhead. The table shows the filesystem; compare
  runs on the same one.
- **No deletes.** SeaweedFS reclaims deleted objects' space only when its
  background vacuum runs, MinIO immediately. After heavy deletes SeaweedFS
  temporarily uses more than this shows.
- **Filer metadata is measured after `sync` with the engine running.**
  SeaweedFS's metadata database may compact later and shrink slightly.

## Configuration

### `bench/.env`

Copy from `.env.example`. Read by both compose files and the scripts.

| Variable | Default | Purpose |
|---|---|---|
| `DRIVE1`–`DRIVE4` | `./drives/d1`–`d4` | Data drives (relative paths are relative to `bench/`). **Wiped before every test.** |
| `MINIO_IMAGE` | `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z-cpuv1` | Set to the production release |
| `MINIO_PARITY` | `2` | Erasure-coding parity (`EC:N`) |
| `MINIO_PORT`, `MINIO_CONSOLE_PORT` | `9100`, `9101` | MinIO ports |
| `SW_IMAGE` | `chrislusf/seaweedfs:4.40` | SeaweedFS release |
| `SW_REPLICATION` | `001` | SeaweedFS replication for writes |
| `SW_VOLUME_SIZE_MB` | `1024` | SeaweedFS volume size limit, both SeaweedFS variants (see [Caveats](#caveats)) |
| `SW_S3_PORT` | `8433` | SeaweedFS S3 port |
| `SW_TUNED_GROWTH_COUNT` | `8` | Physical volumes `sw-tuned` grows at once |
| `SW_TUNED_CACHE_MB` | `1024` | S3 GET chunk cache for `sw-tuned` (counts toward server memory) |
| `WARP_IMAGE` | `minio/warp:v1.3.1` | warp release |
| `BENCH_CPUSET` | `0-11` | CPUs every engine container is pinned to |
| `WARP_CPUSET` | (empty = unpinned) | CPUs warp is pinned to. Must not overlap `BENCH_CPUSET`. Split by physical core, not vCPU number — check `lscpu -p=CPU,CORE,SOCKET`, since hyperthread siblings share execution units. |
| `BENCH_ACCESS_KEY`, `BENCH_SECRET_KEY` | `benchadmin`, `benchadmin-secret` | S3 credentials for both engines |
| `BENCH_DROP_CACHES` | `1` | Drop the page cache between tests (needs passwordless `sudo`) |

### Matrix files

`matrix.env` (default, ~15 minutes), `matrix.small.env` (laptop, ~1 hour)
and `matrix.full.env` (the real benchmark, ~2¼ hours) define what runs, for all
three variants. Pass a matrix file as the second argument to `sweep.sh`;
without one, `matrix.env` is used.

`matrix.small.env` spends its budget on repetitions rather than breadth: one
concurrency level, modest object sizes, but `REPS=3`. On a machine where the
absolute numbers are not trustworthy anyway, a ratio with a stdev beside it is
the only output worth keeping.

| Setting | Default | Small | Full | Meaning |
|---|---|---|---|---|
| `SIZES` | 4KiB 6MiB 16MiB | 4KiB 1MiB 8MiB | 1KiB 128KiB 1MiB 12MiB | Object sizes for PUT and GET |
| `CONCURRENCY` | 16 | 8 | 1 8 16 | Concurrency levels for PUT and GET |
| `DURATION` | 15s | 20s | 1m | Length of each test |
| `SKIP` | 3s | 5s | 10s | Warm-up excluded from results |
| `REPS` | 1 | 3 | 1 | Repetitions of the whole matrix |
| `GET_OBJECTS` | 200 | 200 | 2000 | Objects uploaded before GET and mixed |
| `GET_MAX_BYTES` | 512 MiB | 256 MiB | 1 GiB | Caps `GET_OBJECTS × size` so large sizes fit on the drives and upload quickly |
| `MIXED_SIZE` | 1MiB | 1MiB | 1MiB | Object size in the mixed test |
| `MIXED_CONCURRENCY` | 16 | 8 | 32 | Concurrency for mixed |
| `MIXED_GET/STAT/PUT/DELETE` | 45/30/15/10 | 45/30/15/10 | 45/30/15/10 | Mixed request ratio. `DELETE` must be ≤ `PUT`. |
| `MIXED_RPS_LIMIT` | 120 | 120 | 120 | Total requests per second for mixed, shared by all clients (warp `--rps-limit`). A stand-in until set to production's rate. Empty = unlimited. |
| `MIXED_DURATION` | 1m | 1m | 1m | Length of each mixed test. Empty = `DURATION`. |
| `LONG_MIXED_DURATION` | (empty = skip) | (empty = skip) | (empty = skip) | Long mixed run |

Environment overrides for `sweep.sh`:

| Variable | Default | Purpose |
|---|---|---|
| `BENCH_RUN` | today's date | Results directory name |
| `WARP_MAX_INFLIGHT_BYTES` | 16 GiB | Skip cells where `size × concurrency` would exceed warp's memory budget. `matrix.small.env` (1 GiB) and `matrix.full.env` (2 GiB) lower it; the default is too high for a laptop. |

## Output

```
bench/results/<BENCH_RUN>/
├── summary.md                      # comparison tables (from summarize.py)
├── summary.csv                     # one row per cell, operation and rep
└── <variant>/
    ├── environment.txt             # engine version, CPUs, memory, drives
    ├── matrix.env                  # matrix used for this variant
    ├── bench.env                   # .env used (secret key masked)
    ├── <cell>.csv.zst              # warp raw data: one row per request
    ├── <cell>.log                  # warp's console report
    ├── <cell>.cmd                  # exact warp command
    ├── <cell>.docker-stats.jsonl   # engine + warp CPU/memory, every 5s
    ├── <cell>.iostat.txt           # drive I/O stats, every 5s
    └── storage/                    # storage.sh only
        ├── environment.txt, bench.env
        ├── storage-<size>.json     # objects, bytes uploaded, drives before/after
        └── storage-<size>.{log,cmd,csv.zst}   # warp upload
```

Cells are named `<test>-<size>-c<concurrency>-r<rep>`, for example
`put-4KiB-c16-r1` or `get-16MiB-c16-r1`. Rate-limited mixed cells add the
rate, for example `mixed-1MiB-rps120-c16-r1`, so they never mix with older
unlimited runs.

`summarize.py` with no argument picks the alphabetically last directory
under `results/`, so pass the run directory explicitly.

## Reading the summary

`summary.md` has one table per test and operation. For each size and
concurrency there is a row per variant. A mixed test run at a fixed rate
(`MIXED_RPS_LIMIT`) gets its own section instead, since its throughput is set
by the rate: one table with each variant's achieved rate, whether it kept up
(at least 95% of the target), its engine CPU and errors, then one table of
p50/p90/p99/p99.9 latency per operation (GET, STAT, PUT, DELETE). Engine CPU
is every engine container together, averaged over the `docker stats` samples
inside the measured window; 100% is one vCPU. `summary.csv` has it for every
cell as `engine_cpu_pct`.

A regular table looks like this:

```
| object size | concurrency (clients) | variant       | throughput (ops/s) | throughput (MiB/s) | p50 (ms) | p99 (ms) | p99.9 (ms) | TTFB p99 (ms) | errors (count) | ops/s (% of minio) | p99 (% of minio) |
| 1MiB        | 32                    | minio         | 2,374              | 2,374              | 12.1     | 30.2     | 55.0       | 29.8          | 0              |                    |                  |
| 1MiB        | 32                    | sw-minio-like | 3,100              | 3,100              | 9.0      | 25.1     | 80.3       | 24.0          | 0              | 131%               | 83%              |
```

(Illustrative numbers.)

- **ops/s (% of minio)** above 100% means the variant is faster: 131% is
  1.31 times MinIO's throughput, i.e. 31% more.
- **p99 (% of minio)** below 100% means its tail latency is lower (better):
  83% is 17% less than MinIO's.
- Values show `mean ± stdev` across reps. If the stdev is large relative to
  the difference between variants, the difference isn't meaningful. With
  `REPS=1` there is no stdev, so treat small differences as noise.

What to look at:

1. **Compare `sw-minio-like` against `minio`**, for reads. It's the
   like-for-like number for GETs. For writes it isn't: neither SeaweedFS
   variant syncs S3 PUTs to disk (see
   [How the comparison is kept fair](#how-the-comparison-is-kept-fair)), so
   treat SeaweedFS PUT and mixed-workload numbers as an upper bound.
   `sw-tuned` shows the best case; the gap between the two SeaweedFS variants
   is what the cache and volume growth buy.
2. **Follow each engine across concurrency levels** (full matrix). The point
   where ops/s stops rising and p99 jumps is its practical limit.
3. **Look at small objects and large objects separately.** SeaweedFS is built
   for many small files; MinIO for large objects.
4. **Read the mixed test by latency, not throughput.** With
   `MIXED_RPS_LIMIT` set, every variant is asked for the same request rate, so
   throughput should read "same" and the p50/p99 columns and `docker stats`
   CPU are the comparison. If a variant's total throughput across the four
   operations is clearly below the limit, it could not keep up: that engine
   is saturated at this load, and its latency numbers mean the same as in an
   unlimited run.
5. **Check p99.9 and errors**, not just averages.
6. **Check `docker stats` and `iostat`** for cells with surprising results.
   Disk `%util` near 100% means the drive, not the engine, set the limit.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `WARNING: passwordless sudo unavailable; page cache is NOT dropped` | Set up passwordless `sudo` for `tee /proc/sys/vm/drop_caches`, or set `BENCH_DROP_CACHES=0` to silence it. Results then include reads served from memory. |
| `seaweedfs did not become ready` | Check `docker compose -f bench/seaweedfs-compose.yml logs master filer`. Usually a port is already in use (see [Requirements](#requirements)). |
| `permission denied` in SeaweedFS master logs | The drive directories weren't writable by SeaweedFS's non-root user. `reset_engine` handles this; check the drive paths are on a filesystem that allows `chmod`. |
| `skip: ... exceeds WARP_MAX_INFLIGHT_BYTES` | Expected for large sizes at high concurrency. Raise `WARP_MAX_INFLIGHT_BYTES` if the warp host has the memory. |
| `WARNING: warp exited non-zero` | See `<cell>.log`. The cell has no `.csv.zst`, so rerunning the sweep retries it. |
| Result files owned by root | warp runs as root in its container. Remove them with `sudo rm` or through a container. |

## Caveats

- **Laptop results aren't real results.** On a single-disk machine the four
  "drives" are directories on one device, so neither engine gets real
  multi-drive I/O, and other workloads on the machine add noise.
- **The load generator shares the host.** Set `WARP_CPUSET` to cores outside
  `BENCH_CPUSET` so warp cannot take the engine's. Pinning removes CPU
  contention but not shared L3 cache and memory bandwidth; both engines take
  that hit equally, so a ratio survives it while absolute numbers do not.
  Running warp from another machine removes it entirely, but then the network
  is inside the measurement.
- **4 drives are hard-coded** in both compose files. For another drive count,
  add or remove drive mounts in `minio-compose.yml`, volume services in
  `seaweedfs-compose.yml`, and the loops in `lib.sh` (`drive_paths`,
  `wipe_drives`, the volume-server count in `wait_ready`).
- **SeaweedFS volume slots.** `-max=0` sizes each volume server's volume count
  from free disk space divided by `SW_VOLUME_SIZE_MB`. Writes to one volume
  are serialized, so fewer volumes means less write concurrency. SeaweedFS's
  default of 30 GB leaves small drives with only 1–2 volumes each, a limit
  MinIO doesn't have, so both SeaweedFS variants use 1 GB. Volume size doesn't
  affect durability. On multi-TB drives, 1 GB means thousands of volumes per
  server, each with its own in-memory index; raise it there.
- **Fault tolerance differs.** Replication `001` survives 1 drive failure,
  `EC:2` survives 2. `SW_REPLICATION=002` matches that tolerance at 3x storage.
- **Not covered:** SeaweedFS erasure coding (it targets warm storage and
  never applies to fresh writes), multipart uploads, listing, standalone HEAD
  or DELETE benchmarks (HEAD and DELETE only appear inside the mixed test),
  S3 feature compatibility (see `run_test.py` in the repository root),
  failure recovery and operations.

## Files

| File | Purpose |
|---|---|
| `sweep.sh` | Runs the matrix for one variant |
| `storage.sh` | Measures disk space and inodes used per byte stored, for one variant |
| `lib.sh` | Shared helpers: engine reset, warp runs, metrics, environment capture |
| `summarize.py` | Builds `summary.csv` and `summary.md` from a run |
| `minio-compose.yml` | Single-node, 4-drive MinIO |
| `seaweedfs-compose.yml` | SeaweedFS: master, 4 volume servers, filer with S3 |
| `matrix.env` | Default matrix: first impression in ~15 minutes |
| `matrix.small.env` | Laptop matrix: fewer cells, 3 reps, ~1 hour for all three variants |
| `matrix.full.env` | The real benchmark on the AWS server (~2¼ hours for all three variants) |
| `.env.example` | Settings template |
| `.env.m5d-xlarge` | Settings template for an EC2 `m5d.xlarge` benchmark server |
| `s3.generated.json` | SeaweedFS S3 credentials, generated from `.env` (gitignored) |
