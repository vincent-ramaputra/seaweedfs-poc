#!/usr/bin/env python3
"""Summarize a benchmark run into comparison tables.

    python3 bench/summarize.py [results/<run> dir]

Reads every <variant>/<bench>-<param>-c<conc>-r<rep>.csv.zst written by
sweep.sh (warp --full output: one row per request), drops warp's preparation
uploads and the warm-up period (SKIP from the matrix used for that variant),
and computes exact throughput, latency percentiles and TTFB per operation.

Writes <run>/summary.csv (one row per rep) and <run>/summary.md (mean ± stdev
across reps, grouped per benchmark and operation, each variant compared
against minio as "N× faster" / "N× slower"). Mixed cells run at a fixed rate
(MIXED_RPS_LIMIT) get their own section: whether each variant kept up with the
rate, its engine CPU from <cell>.docker-stats.jsonl, and latency per operation.
Results of storage.sh (<variant>/storage/*.json) become a "Storage used" table.
Requires the zstd binary.
"""

import csv
import io
import json
import math
import re
import statistics
import subprocess
import sys
from array import array
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
NAME_RE = re.compile(r"^(?P<bench>[a-z]+)-(?P<param>.+)-c(?P<conc>\d+)-r(?P<rep>\d+)$")
VARIANT_ORDER = ["minio", "sw-minio-like", "sw-tuned"]
BASELINE = "minio"
CSV_FIELDS = [
    "bench", "param", "conc", "op", "variant", "rep", "requests", "errors",
    "ops_per_s", "mib_per_s", "p50_ms", "p90_ms", "p99_ms", "p999_ms",
    "ttfb_p50_ms", "ttfb_p99_ms", "engine_cpu_pct",
]
# A fixed-rate cell's param ends in "-rps<N>" (see MIXED_RPS_LIMIT in sweep.sh).
RPS_RE = re.compile(r"^(?P<size>.+)-rps(?P<rps>\d+(?:\.\d+)?)$")
# A variant counts as keeping up when it reaches this share of the target rate.
KEPT_UP = 0.95
# Order of the mixed operations by their share of traffic.
MIXED_OP_ORDER = ["GET", "STAT", "PUT", "DELETE"]
# docker stats --no-stream measures CPU over roughly this long after its timestamp.
STATS_SAMPLE_S = 2

_epoch_cache: dict[str, int] = {}


def to_ns(ts: str) -> int:
    """'2026-09-14T07:54:31.77643397Z' -> epoch nanoseconds."""
    ts = ts.rstrip("Z")
    base, _, frac = ts.partition(".")
    sec = _epoch_cache.get(base)
    if sec is None:
        sec = int(datetime.fromisoformat(base).replace(tzinfo=timezone.utc).timestamp())
        _epoch_cache[base] = sec
    return sec * 1_000_000_000 + int((frac + "000000000")[:9])


def parse_duration_s(value: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", value.strip())
    if not m:
        raise ValueError(f"cannot parse duration {value!r}")
    scale = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, None: 1}[m.group(2)]
    return float(m.group(1)) * scale


def read_skip_s(variant_dir: Path) -> float:
    matrix = variant_dir / "matrix.env"
    if matrix.exists():
        for line in matrix.read_text().splitlines():
            if line.startswith("SKIP="):
                return parse_duration_s(line.split("=", 1)[1])
    return 0.0


def percentile(sorted_values, pct: float) -> float:
    """Nearest-rank percentile."""
    if not sorted_values:
        return math.nan
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def read_requests(path: Path):
    proc = subprocess.run(["zstd", "-dc", str(path)], check=True, capture_output=True)
    lines = (ln for ln in io.StringIO(proc.stdout.decode()) if not ln.startswith("#"))
    yield from csv.DictReader(lines, delimiter="\t")


def engine_cpu_pct(stats_path: Path, start_ns: int, end_ns: int) -> float:
    """Mean CPU of all engine containers together, over the benchmark window.

    100% is one vCPU. Uses the <cell>.docker-stats.jsonl samples written by
    lib.sh (one "<epoch> <json>" line per container per sample), excluding warp.
    """
    if not stats_path.exists():
        return math.nan
    per_sample = defaultdict(float)
    for line in stats_path.read_text().splitlines():
        ts, _, js = line.partition(" ")
        try:
            t, stats = int(ts), json.loads(js)
        except ValueError:
            continue
        if stats.get("Name") == "bench-warp":
            continue
        if start_ns <= t * 1_000_000_000 and (t + STATS_SAMPLE_S) * 1_000_000_000 <= end_ns:
            per_sample[t] += float(stats["CPUPerc"].rstrip("%"))
    return statistics.fmean(per_sample.values()) if per_sample else math.nan


def analyze_file(path: Path, skip_s: float) -> list[dict]:
    rows = list(read_requests(path))
    if not rows:
        return []
    starts = [to_ns(r["start"]) for r in rows]
    ops = {r["op"] for r in rows}
    # warp uploads objects with PUT before GET/STAT/DELETE/LIST/mixed benchmarks.
    # The benchmark window starts at the first request of any other operation.
    if ops - {"PUT"}:
        window_start = min(s for s, r in zip(starts, rows) if r["op"] != "PUT")
    else:
        window_start = min(starts)
    t0 = window_start + int(skip_s * 1e9)

    per_op = defaultdict(lambda: {"dur": array("q"), "ttfb": array("q"), "bytes": 0, "errors": 0, "end": 0})
    for start, r in zip(starts, rows):
        if start < t0:
            continue
        s = per_op[r["op"]]
        if r["error"]:
            s["errors"] += 1
            continue
        s["dur"].append(int(r["duration_ns"]))
        if r["first_byte"]:
            s["ttfb"].append(to_ns(r["first_byte"]) - start)
        s["bytes"] += int(r["bytes"] or 0)
        s["end"] = max(s["end"], to_ns(r["end"]))

    last_end = max((s["end"] for s in per_op.values()), default=0)
    span_s = (last_end - t0) / 1e9
    cpu = engine_cpu_pct(path.with_name(path.name.replace(".csv.zst", ".docker-stats.jsonl")), t0, last_end)
    results = []
    for op, s in sorted(per_op.items()):
        dur = sorted(s["dur"])
        ttfb = sorted(s["ttfb"])
        ms = lambda v: v / 1e6  # noqa: E731
        results.append({
            "op": op,
            "requests": len(dur),
            "errors": s["errors"],
            "ops_per_s": len(dur) / span_s if span_s > 0 else math.nan,
            "mib_per_s": s["bytes"] / 2**20 / span_s if span_s > 0 else math.nan,
            "p50_ms": ms(percentile(dur, 50)),
            "p90_ms": ms(percentile(dur, 90)),
            "p99_ms": ms(percentile(dur, 99)),
            "p999_ms": ms(percentile(dur, 99.9)),
            "ttfb_p50_ms": ms(percentile(ttfb, 50)) if ttfb else math.nan,
            "ttfb_p99_ms": ms(percentile(ttfb, 99)) if ttfb else math.nan,
            "engine_cpu_pct": cpu,
        })
    return results


def size_key(param: str):
    # param is an object size, optionally followed by a suffix such as "-rps120".
    m = re.fullmatch(r"(\d+)(KiB|MiB|GiB)?(?:-.*)?", param)
    if not m:
        return (math.inf, param)
    mult = {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, None: 1}[m.group(2)]
    return (int(m.group(1)) * mult, param)


def fmt(values, digits=1):
    values = [v for v in values if not math.isnan(v)]
    if not values:
        return "–"
    mean = statistics.fmean(values)
    if len(values) > 1:
        return f"{mean:,.{digits}f} ± {statistics.stdev(values):,.{digits}f}"
    return f"{mean:,.{digits}f}"


def mean_or_nan(values):
    values = [v for v in values if not math.isnan(v)]
    return statistics.fmean(values) if values else math.nan


def speedup(value, base, higher_is_better: bool):
    """How much faster value is than base, as '2.13× faster' or '1.43× slower'.

    Throughput is better when higher, latency when lower; the result always reads
    the same way so the two columns can be compared at a glance.
    """
    if math.isnan(value) or math.isnan(base) or value == 0 or base == 0:
        return "–"
    factor = value / base if higher_is_better else base / value
    if abs(factor - 1) < 0.05:
        return f"same ({factor:.2f}×)"
    if factor > 1:
        return f"**{factor:.2f}× faster**"
    return f"{1 / factor:.2f}× slower"


def write_markdown(rows: list[dict], path: Path, storage: list[dict]) -> None:
    # (bench, op) -> (param, conc) -> variant -> [rows across reps]
    groups = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    fixed_rate = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if RPS_RE.match(r["param"]):
            fixed_rate[(r["bench"], r["param"], int(r["conc"]))][r["variant"]].append(r)
        else:
            groups[(r["bench"], r["op"])][(r["param"], int(r["conc"]))][r["variant"]].append(r)

    out = ["# Benchmark summary", ""]
    if rows:
        out += [
            "Mean ± stdev across reps. Warm-up and preparation uploads excluded.",
            f"The last two columns compare each variant with `{BASELINE}` at the same object size and concurrency. "
            "Both read the same way: \"2.00× faster\" means twice the throughput, or half the p99 latency. "
            "Differences under 5% are shown as \"same\".",
            "",
        ]
    for (bench, op) in sorted(groups):
        out += [
            f"## {bench} — {op}",
            "",
            "| object size | concurrency (clients) | variant | throughput (ops/s) | throughput (MiB/s) "
            "| p50 (ms) | p99 (ms) | p99.9 (ms) | TTFB p99 (ms) | errors (count) "
            f"| throughput vs {BASELINE} | p99 latency vs {BASELINE} |",
            "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        cells = groups[(bench, op)]
        for param, conc in sorted(cells, key=lambda k: (size_key(k[0]), k[1])):
            by_variant = cells[(param, conc)]
            base = by_variant.get(BASELINE, [])
            base_ops = mean_or_nan([r["ops_per_s"] for r in base])
            base_p99 = mean_or_nan([r["p99_ms"] for r in base])
            variants = order_variants(by_variant)
            for v in variants:
                rs = by_variant[v]
                ops = [r["ops_per_s"] for r in rs]
                p99 = [r["p99_ms"] for r in rs]
                is_base = v == BASELINE
                out.append(
                    f"| {param} | {conc} | {v} | {fmt(ops)} | {fmt([r['mib_per_s'] for r in rs], 2)} "
                    f"| {fmt([r['p50_ms'] for r in rs], 2)} | {fmt(p99, 2)} | {fmt([r['p999_ms'] for r in rs], 2)} "
                    f"| {fmt([r['ttfb_p99_ms'] for r in rs], 2)} | {sum(r['errors'] for r in rs)} "
                    f"| {'baseline' if is_base else speedup(mean_or_nan(ops), base_ops, True)} "
                    f"| {'baseline' if is_base else speedup(mean_or_nan(p99), base_p99, False)} |"
                )
        out.append("")
    for key in sorted(fixed_rate):
        out += fixed_rate_section(*key, fixed_rate[key])
    out += storage_section(storage)
    path.write_text("\n".join(out))


def order_variants(variants):
    return sorted(variants, key=lambda v: (VARIANT_ORDER.index(v) if v in VARIANT_ORDER else 99, v))


def fixed_rate_section(bench: str, param: str, conc: int, by_variant: dict) -> list[str]:
    """Tables for one mixed cell run at a fixed request rate.

    Every variant was sent the same rate, so throughput is not a result here:
    the section shows whether each variant kept up, its CPU, and its latency.
    """
    m = RPS_RE.match(param)
    size, target = m["size"], float(m["rps"])
    variants = order_variants(by_variant)
    out = [
        f"## {bench} at a fixed rate — {size} objects, {target:g} req/s, {conc} clients",
        "",
        "Every variant was sent the same request rate, so compare latency and CPU, not throughput. "
        f"A variant below {KEPT_UP:.0%} of the target rate could not keep up: it was saturated, "
        "and its latencies are not comparable with the others. "
        "Engine CPU is all engine containers together over the measured window; 100% is one vCPU.",
        "",
        "| variant | achieved (req/s) | kept up | engine CPU (%) | errors (count) |",
        "|---|---:|---|---:|---:|",
    ]
    for v in variants:
        by_rep = defaultdict(list)
        for r in by_variant[v]:
            by_rep[r["rep"]].append(r)
        achieved = [sum(r["ops_per_s"] for r in rs) for rs in by_rep.values()]
        cpu = [rs[0]["engine_cpu_pct"] for rs in by_rep.values()]
        kept_up = "yes" if statistics.fmean(achieved) >= KEPT_UP * target else "**no**"
        errors = sum(r["errors"] for r in by_variant[v])
        out.append(f"| {v} | {fmt(achieved)} | {kept_up} | {fmt(cpu, 0)} | {errors} |")

    out += [
        "",
        "| operation | variant | p50 (ms) | p90 (ms) | p99 (ms) | p99.9 (ms) | TTFB p99 (ms) "
        f"| p50 latency vs {BASELINE} | p99 latency vs {BASELINE} |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    ops = {r["op"] for rs in by_variant.values() for r in rs}
    for op in sorted(ops, key=lambda o: (MIXED_OP_ORDER.index(o) if o in MIXED_OP_ORDER else 99, o)):
        per_v = {v: [r for r in by_variant[v] if r["op"] == op] for v in variants}
        base = per_v.get(BASELINE, [])
        base_p50 = mean_or_nan([r["p50_ms"] for r in base])
        base_p99 = mean_or_nan([r["p99_ms"] for r in base])
        for v in variants:
            rs = per_v[v]
            if not rs:
                continue
            is_base = v == BASELINE
            p50 = mean_or_nan([r["p50_ms"] for r in rs])
            p99 = mean_or_nan([r["p99_ms"] for r in rs])
            out.append(
                f"| {op} | {v} | {fmt([r['p50_ms'] for r in rs], 2)} | {fmt([r['p90_ms'] for r in rs], 2)} "
                f"| {fmt([r['p99_ms'] for r in rs], 2)} | {fmt([r['p999_ms'] for r in rs], 2)} "
                f"| {fmt([r['ttfb_p99_ms'] for r in rs], 2)} "
                f"| {'baseline' if is_base else speedup(p50, base_p50, False)} "
                f"| {'baseline' if is_base else speedup(p99, base_p99, False)} |"
            )
    out.append("")
    return out


def ratio_less(value, base):
    """How much less space value uses than base, as '2.10× less' or '1.30× more'."""
    if math.isnan(value) or math.isnan(base) or value <= 0 or base <= 0:
        return "–"
    factor = base / value
    if abs(factor - 1) < 0.05:
        return f"same ({factor:.2f}×)"
    if factor > 1:
        return f"**{factor:.2f}× less**"
    return f"{1 / factor:.2f}× more"


def storage_rows(run_dir: Path) -> list[dict]:
    """One row per variant and object size from storage.sh's JSON files."""
    rows = []
    for f in sorted(run_dir.glob("*/storage/storage-*.json")):
        d = json.loads(f.read_text())
        total = lambda key, when: sum(x[key] for x in d[when])  # noqa: E731
        logical = d["logical_bytes"]
        used = total("used_bytes", "full") - total("used_bytes", "empty")
        apparent = total("apparent_bytes", "full") - total("apparent_bytes", "empty")
        inodes = total("inodes", "full") - total("inodes", "empty")
        rows.append({
            "variant": d["variant"], "size": d["size"], "size_bytes": d["size_bytes"],
            "objects": d["objects"], "logical_bytes": logical, "filesystem": d["filesystem"],
            "used_bytes": used,
            "used_ratio": used / logical if logical else math.nan,
            "apparent_ratio": apparent / logical if logical else math.nan,
            "inodes_per_object": inodes / d["objects"] if d["objects"] else math.nan,
        })
    return rows


def storage_section(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    fs = sorted({r["filesystem"] for r in rows})
    out = [
        "## Storage used",
        "",
        "From `storage.sh`: a fixed set of objects uploaded and kept, then the drives measured "
        "after `sync`, minus what the empty engine used. "
        "\"Disk used ÷ data\" is the real storage overhead, including filesystem block rounding; "
        "both engines are configured for 2× (EC:2 on 4 drives, replication 001), so 2.00 means no "
        "overhead beyond that. \"File content ÷ data\" leaves block rounding out. "
        f"Inodes are files plus directories. Filesystem: {', '.join(fs)}.",
        "",
        "| object size | variant | objects | data stored (MiB) | disk used (MiB) | disk used ÷ data "
        f"| file content ÷ data | inodes per object | disk used vs {BASELINE} |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    by_size = defaultdict(dict)
    for r in rows:
        by_size[(r["size_bytes"], r["size"])][r["variant"]] = r
    for key in sorted(by_size):
        variants = by_size[key]
        base = variants.get(BASELINE)
        for v in order_variants(variants):
            r = variants[v]
            vs = "baseline" if v == BASELINE else ratio_less(r["used_bytes"], base["used_bytes"] if base else math.nan)
            out.append(
                f"| {r['size']} | {v} | {r['objects']:,} | {r['logical_bytes'] / 2**20:,.1f} "
                f"| {r['used_bytes'] / 2**20:,.1f} | {r['used_ratio']:.2f} | {r['apparent_ratio']:.2f} "
                f"| {r['inodes_per_object']:.1f} | {vs} |"
            )
    out.append("")
    return out


def main() -> int:
    if len(sys.argv) > 1:
        run_dir = Path(sys.argv[1]).resolve()
    else:
        runs = sorted(p for p in (BENCH_DIR / "results").glob("*") if p.is_dir())
        if not runs:
            print("no results found under bench/results", file=sys.stderr)
            return 1
        run_dir = runs[-1]

    rows = []
    storage = storage_rows(run_dir)
    for variant_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        skip_s = read_skip_s(variant_dir)
        for f in sorted(variant_dir.glob("*.csv.zst")):
            name = f.name.removesuffix(".csv.zst")
            m = NAME_RE.match(name)
            if not m:
                print(f"skipping unrecognized file {f}", file=sys.stderr)
                continue
            print(f"{variant_dir.name}/{name}", file=sys.stderr)
            for res in analyze_file(f, skip_s):
                rows.append({**m.groupdict(), "variant": variant_dir.name, **res})

    if not rows and not storage:
        print(f"no benchmark data in {run_dir}", file=sys.stderr)
        return 1

    with open(run_dir / "summary.csv", "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    write_markdown(rows, run_dir / "summary.md", storage)
    print(f"wrote {run_dir / 'summary.csv'} and {run_dir / 'summary.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
