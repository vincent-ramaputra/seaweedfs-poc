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
| `sw-r001` | SeaweedFS, one volume server per drive, every object stored twice (replication `001`) | How fast is SeaweedFS at the same 2x storage overhead? |
| `sw-r001-fsync` | Same, but every write is synced to disk before replying | How much of the difference is only disk syncing? |

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
- **Disk syncing measured both ways.** MinIO syncs each write to disk.
  SeaweedFS doesn't unless `fs.configure -fsync` is set, which makes its writes
  look faster than a like-for-like comparison would. `sw-r001-fsync` shows the
  like-for-like number.
- **Same CPUs.** Every container is pinned to `BENCH_CPUSET`.
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
| **Mixed** | GET, HEAD, PUT and DELETE at the same time, in a set ratio | Behavior under realistic traffic | concurrency |
| **Long mixed** | Mixed workload for a long period (full matrix only) | Latency drift and background work (SeaweedFS vacuum, MinIO scanner) | once |

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
| **vs minio** | Ratio against MinIO for the same cell |

Percentiles are computed exactly from every recorded request (warp `--full`),
not approximated from warp's per-segment summaries.

Each cell also records container CPU/memory (`docker stats`) and disk load
(`iostat`), to tell whether the engine or the disk was the bottleneck.

## Requirements

- Docker with Compose v2
- `curl`, `jq`, `zstd`, `iostat` (package `sysstat`) and Python 3.10+ on the host
- Free host ports (all engines use host networking):

  | Engine | Ports |
  |---|---|
  | MinIO | 9100 (S3), 9101 (console) |
  | SeaweedFS | 9433, 19433, 9434 (master) · 8481–8484, 18481–18484, 9481–9484 (volume servers) · 8988, 18988, 9489 (filer) · 8433, 18433, 9490 (S3) |

- Optional: passwordless `sudo`, to drop the page cache between tests

Images used: `minio/minio`, `chrislusf/seaweedfs`, `minio/warp`, set in `.env`.

## Quick run

The default matrix (`matrix.env`) gives a first impression in **about 12
minutes for all three variants**. It runs PUT and GET on small (4 KiB) and
large (16 MiB) objects plus one mixed workload, at a single concurrency
level (16), 15 seconds per test, once. It also checks that the harness works.

It is too short and too narrow to base a decision on: there is no
concurrency curve, no repetitions to show variance, and each test measures
only ~12 seconds. Use the [full benchmark](#full-benchmark) for decisions.

```bash
cp bench/.env.example bench/.env        # defaults work on a dev machine

export BENCH_RUN=quick-$(date +%Y%m%d-%H%M)
for v in minio sw-r001 sw-r001-fsync; do
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
2. **Isolate the load generator.** Set `BENCH_CPUSET` to cores used by the
   engines only, and keep warp and other workloads off them. Ideally run warp
   from a separate machine.
3. **Set the production traffic mix.** In `bench/matrix.full.env`, set
   `MIXED_GET`, `MIXED_STAT`, `MIXED_PUT`, `MIXED_DELETE` to production's
   request ratio, and adjust `SIZES` to production's object sizes.
4. **Run every variant** under the same run name, inside `tmux` or `screen`.
   See [Duration and disk space](#duration-and-disk-space) first: this takes
   days.
   ```bash
   export BENCH_RUN=$(date +%Y%m%d)
   for v in minio sw-r001 sw-r001-fsync; do
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
| `matrix.env` (default) | 5 × 15 s | ~4 min | **~12 min** |
| `matrix.full.env` | ~186 × 5 min (62 per rep × 3 reps), plus a 90 min long mixed run | ~1 day | **~3 days** |

Besides the measured time, each cell spends time restarting the engine
(~10 s), uploading setup data and cleaning up; that overhead is included
above.

Time scales with the number of cells: each size, concurrency level or rep
you add multiplies it. A middle ground, such as the default sizes with
`CONCURRENCY="8 32 128"`, `DURATION=1m` and `REPS=2`, takes a few hours.

**Disk space.** The drives need room for `GET_MAX_BYTES` × the storage
overhead (2x for both `EC:2` and replication `001`) plus headroom:

| Matrix | `GET_MAX_BYTES` | Free space needed across the drives |
|---|---|---|
| `matrix.env` | 512 MiB | ~2 GB |
| `matrix.full.env` | 200 GiB | ~500 GB |

The drives default to directories under `bench/`, so on a dev machine this
space comes out of that filesystem. Don't run `matrix.full.env` on a laptop.

**Resuming.** A sweep skips cells that already have results, so after an
interruption, rerun the same command with the same `BENCH_RUN`. To redo a
cell, delete its files first (`<cell>.*`).

**Stopping the engines.** A sweep leaves the last engine running for
inspection. Stop both with:

```bash
docker compose -f bench/minio-compose.yml down
docker compose -f bench/seaweedfs-compose.yml down
```

## Configuration

### `bench/.env`

Copy from `.env.example`. Read by both compose files and the scripts.

| Variable | Default | Purpose |
|---|---|---|
| `DRIVE1`–`DRIVE4` | `./drives/d1`–`d4` | Data drives (relative paths are relative to `bench/`). **Wiped before every test.** |
| `MINIO_IMAGE` | `minio/minio:RELEASE.2025-09-07T16-13-09Z-cpuv1` | Set to the production release |
| `MINIO_PARITY` | `2` | Erasure-coding parity (`EC:N`) |
| `MINIO_PORT`, `MINIO_CONSOLE_PORT` | `9100`, `9101` | MinIO ports |
| `SW_IMAGE` | `chrislusf/seaweedfs:4.40` | SeaweedFS release |
| `SW_REPLICATION` | `001` | SeaweedFS replication for writes |
| `SW_VOLUME_SIZE_MB` | `30000` | SeaweedFS volume size limit |
| `SW_S3_PORT` | `8433` | SeaweedFS S3 port |
| `WARP_IMAGE` | `minio/warp:v1.3.1` | warp release |
| `BENCH_CPUSET` | `0-11` | CPUs every engine container is pinned to |
| `BENCH_ACCESS_KEY`, `BENCH_SECRET_KEY` | `benchadmin`, `benchadmin-secret` | S3 credentials for both engines |
| `BENCH_DROP_CACHES` | `1` | Drop the page cache between tests (needs passwordless `sudo`) |

### Matrix files

`matrix.env` (default, ~12 minutes) and `matrix.full.env` (exhaustive, days)
define what runs. Pass a matrix file as the second argument to `sweep.sh`;
without one, `matrix.env` is used.

| Setting | Default | Full | Meaning |
|---|---|---|---|
| `SIZES` | 4KiB 16MiB | 1KiB … 1GiB | Object sizes for PUT and GET |
| `CONCURRENCY` | 16 | 1 8 32 128 512 | Concurrency levels for PUT and GET |
| `DURATION` | 15s | 5m | Length of each test |
| `SKIP` | 3s | 30s | Warm-up excluded from results |
| `REPS` | 1 | 3 | Repetitions of the whole matrix |
| `GET_OBJECTS` | 200 | 10000 | Objects uploaded before GET and mixed |
| `GET_MAX_BYTES` | 512 MiB | 200 GiB | Caps `GET_OBJECTS × size` so large sizes fit on the drives |
| `MIXED_SIZE` | 1MiB | 1MiB | Object size in the mixed test |
| `MIXED_CONCURRENCY` | 16 | 32 128 | Concurrency for mixed |
| `MIXED_GET/STAT/PUT/DELETE` | 45/30/15/10 | 45/30/15/10 | Mixed request ratio. `DELETE` must be ≤ `PUT`. |
| `LONG_MIXED_DURATION` | (empty = skip) | 90m | Long mixed run |

Environment overrides for `sweep.sh`:

| Variable | Default | Purpose |
|---|---|---|
| `BENCH_RUN` | today's date | Results directory name |
| `WARP_MAX_INFLIGHT_BYTES` | 16 GiB | Skip cells where `size × concurrency` would exceed warp's memory budget |

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
    ├── <cell>.docker-stats.jsonl   # container CPU/memory, every 5s
    └── <cell>.iostat.txt           # drive I/O stats, every 5s
```

Cells are named `<test>-<size>-c<concurrency>-r<rep>`, for example
`put-4KiB-c16-r1`, `get-16MiB-c16-r1` or `mixed-1MiB-c16-r1`.

`summarize.py` with no argument picks the alphabetically last directory
under `results/`, so pass the run directory explicitly.

## Reading the summary

`summary.md` has one table per test and operation. The mixed test gets one
table per operation it performs (GET, STAT, PUT, DELETE). For each size and
concurrency there is a row per variant:

```
| param | conc | variant       | ops/s  | MiB/s | p50  | p99  | p99.9 | TTFB p99 | errors | ops/s vs minio | p99 vs minio |
| 1MiB  | 32   | minio         | 2,374  | 2,374 | 12.1 | 30.2 | 55.0  | 29.8     | 0      |                |              |
| 1MiB  | 32   | sw-r001       | 3,100  | 3,100 | 9.0  | 25.1 | 80.3  | 24.0     | 0      | 1.31×          | 0.83×        |
```

(Illustrative numbers.)

- **ops/s vs minio** above 1× means the variant is faster.
- **p99 vs minio** below 1× means its tail latency is lower (better).
- Values show `mean ± stdev` across reps. If the stdev is large relative to
  the difference between variants, the difference isn't meaningful. With
  `REPS=1` there is no stdev, so treat small differences as noise.

What to look at:

1. **Compare `sw-r001-fsync` against `minio` for writes.** It's the
   like-for-like number. `sw-r001` shows the best case without syncing.
2. **Follow each engine across concurrency levels** (full matrix). The point
   where ops/s stops rising and p99 jumps is its practical limit.
3. **Look at small objects and large objects separately.** SeaweedFS is built
   for many small files; MinIO for large objects.
4. **Check p99.9 and errors**, not just averages.
5. **Check `docker stats` and `iostat`** for cells with surprising results.
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
- **The load generator shares the host by default.** On the real benchmark,
  run warp from another machine, or at least on cores outside `BENCH_CPUSET`.
- **4 drives are hard-coded** in both compose files. For another drive count,
  add or remove drive mounts in `minio-compose.yml`, volume services in
  `seaweedfs-compose.yml`, and the loops in `lib.sh` (`drive_paths`,
  `wipe_drives`, the volume-server count in `wait_ready`).
- **SeaweedFS volume slots.** `-max=0` sizes each volume server's volume count
  from free disk space divided by `SW_VOLUME_SIZE_MB`. Small drives get very
  few writable volumes, which can limit write concurrency.
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
| `lib.sh` | Shared helpers: engine reset, warp runs, metrics, environment capture |
| `summarize.py` | Builds `summary.csv` and `summary.md` from a run |
| `minio-compose.yml` | Single-node, 4-drive MinIO |
| `seaweedfs-compose.yml` | SeaweedFS: master, 4 volume servers, filer with S3 |
| `matrix.env` | Default matrix: first impression in ~12 minutes |
| `matrix.full.env` | Exhaustive matrix for a production-like server (days) |
| `.env.example` | Settings template |
| `s3.generated.json` | SeaweedFS S3 credentials, generated from `.env` (gitignored) |
