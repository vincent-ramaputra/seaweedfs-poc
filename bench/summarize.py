#!/usr/bin/env python3
"""Summarize a benchmark run into comparison tables.

    python3 bench/summarize.py [results/<run> dir]

Reads every <variant>/<bench>-<param>-c<conc>-r<rep>.csv.zst written by
sweep.sh (warp --full output: one row per request), drops warp's preparation
uploads and the warm-up period (SKIP from the matrix used for that variant),
and computes exact throughput, latency percentiles and TTFB per operation.

Writes <run>/summary.csv (one row per rep) and <run>/summary.md (mean ± stdev
across reps, grouped per benchmark and operation, with ratios against minio).
Requires the zstd binary.
"""

import csv
import io
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
VARIANT_ORDER = ["minio", "sw-r001", "sw-r001-fsync"]
BASELINE = "minio"
CSV_FIELDS = [
    "bench", "param", "conc", "op", "variant", "rep", "requests", "errors",
    "ops_per_s", "mib_per_s", "p50_ms", "p90_ms", "p99_ms", "p999_ms",
    "ttfb_p50_ms", "ttfb_p99_ms",
]

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
        })
    return results


def size_key(param: str):
    m = re.fullmatch(r"(\d+)(KiB|MiB|GiB)?", param)
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


def ratio(value, base):
    if math.isnan(value) or math.isnan(base) or base == 0:
        return "–"
    return f"{value / base:.2f}×"


def write_markdown(rows: list[dict], path: Path) -> None:
    # (bench, op) -> (param, conc) -> variant -> [rows across reps]
    groups = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in rows:
        groups[(r["bench"], r["op"])][(r["param"], int(r["conc"]))][r["variant"]].append(r)

    out = [
        "# Benchmark summary",
        "",
        "Mean ± stdev across reps. Latencies in ms. Warm-up and preparation uploads excluded.",
        f"Ratios compare against `{BASELINE}`: throughput above 1× is faster, p99 below 1× is faster.",
        "",
    ]
    for (bench, op) in sorted(groups):
        out += [
            f"## {bench} — {op}",
            "",
            "| param | conc | variant | ops/s | MiB/s | p50 | p99 | p99.9 | TTFB p99 | errors | ops/s vs minio | p99 vs minio |",
            "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        cells = groups[(bench, op)]
        for param, conc in sorted(cells, key=lambda k: (size_key(k[0]), k[1])):
            by_variant = cells[(param, conc)]
            base = by_variant.get(BASELINE, [])
            base_ops = mean_or_nan([r["ops_per_s"] for r in base])
            base_p99 = mean_or_nan([r["p99_ms"] for r in base])
            variants = sorted(by_variant, key=lambda v: (VARIANT_ORDER.index(v) if v in VARIANT_ORDER else 99, v))
            for v in variants:
                rs = by_variant[v]
                ops = [r["ops_per_s"] for r in rs]
                p99 = [r["p99_ms"] for r in rs]
                is_base = v == BASELINE
                out.append(
                    f"| {param} | {conc} | {v} | {fmt(ops)} | {fmt([r['mib_per_s'] for r in rs], 2)} "
                    f"| {fmt([r['p50_ms'] for r in rs], 2)} | {fmt(p99, 2)} | {fmt([r['p999_ms'] for r in rs], 2)} "
                    f"| {fmt([r['ttfb_p99_ms'] for r in rs], 2)} | {sum(r['errors'] for r in rs)} "
                    f"| {'' if is_base else ratio(mean_or_nan(ops), base_ops)} "
                    f"| {'' if is_base else ratio(mean_or_nan(p99), base_p99)} |"
                )
        out.append("")
    path.write_text("\n".join(out))


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

    if not rows:
        print(f"no benchmark data in {run_dir}", file=sys.stderr)
        return 1

    with open(run_dir / "summary.csv", "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    write_markdown(rows, run_dir / "summary.md")
    print(f"wrote {run_dir / 'summary.csv'} and {run_dir / 'summary.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
