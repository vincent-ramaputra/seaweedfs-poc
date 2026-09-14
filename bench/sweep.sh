#!/usr/bin/env bash
# Run the benchmark matrix against one engine variant.
#
#   bench/sweep.sh <variant> [matrix file]
#
# Variants: minio, sw-r001, sw-r001-fsync.
# Results go to bench/results/$BENCH_RUN/<variant>/ (BENCH_RUN defaults to
# today's date). Cells that already have results are skipped, so an
# interrupted sweep can be resumed by rerunning the same command.
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

variant=${1:?usage: sweep.sh <minio|sw-r001|sw-r001-fsync> [matrix file]}
matrix=$(realpath "${2:-$BENCH_DIR/matrix.env}")
engine_of "$variant" >/dev/null
# shellcheck disable=SC1090
source "$matrix"

# warp keeps each in-flight object in memory; skip cells that would need more.
: "${WARP_MAX_INFLIGHT_BYTES:=$((16 * 1024 * 1024 * 1024))}"

out="$BENCH_DIR/results/${BENCH_RUN:-$(date +%Y%m%d)}/$variant"
mkdir -p "$out"
trap stop_metrics EXIT

size_bytes() {
  local s=$1
  case "$s" in
    *KiB) echo $((${s%KiB} * 1024)) ;;
    *MiB) echo $((${s%MiB} * 1024 * 1024)) ;;
    *GiB) echo $((${s%GiB} * 1024 * 1024 * 1024)) ;;
    *) echo "$s" ;;
  esac
}

# Objects to prepare for GET/mixed: GET_OBJECTS, capped by GET_MAX_BYTES.
object_count() {
  local cap=$((GET_MAX_BYTES / $(size_bytes "$1")))
  ((cap < 16)) && cap=16
  ((GET_OBJECTS < cap)) && echo "$GET_OBJECTS" || echo "$cap"
}

fits_in_memory() {
  local bytes=$1 conc=$2
  if ((bytes * conc > WARP_MAX_INFLIGHT_BYTES)); then
    log "skip: $conc x $bytes bytes in flight exceeds WARP_MAX_INFLIGHT_BYTES"
    return 1
  fi
}

# cell <name> <warp op> [flags...]
# Runs one warp benchmark against a freshly reset engine.
cell() {
  local name=$1
  shift
  if compgen -G "$out/$name.*.zst" >/dev/null; then
    log "skip $name (already done)"
    return 0
  fi
  reset_engine "$variant"
  start_metrics "$out/$name"
  run_warp "$variant" "$out" "$name" "$@"
  stop_metrics
}

# mixed_cell <name> <concurrency> <duration>
mixed_cell() {
  cell "$1" mixed \
    --obj.size "$MIXED_SIZE" --objects "$(object_count "$MIXED_SIZE")" --concurrent "$2" \
    --get-distrib "$MIXED_GET" --stat-distrib "$MIXED_STAT" \
    --put-distrib "$MIXED_PUT" --delete-distrib "$MIXED_DELETE" \
    --duration "$3"
}

record_environment "$variant" "$out" "$matrix"
log "variant $variant, matrix $matrix, results in $out"

for rep in $(seq 1 "$REPS"); do
  for size in $SIZES; do
    for c in $CONCURRENCY; do
      fits_in_memory "$(size_bytes "$size")" "$c" || continue
      cell "put-$size-c$c-r$rep" put \
        --obj.size "$size" --concurrent "$c" --duration "$DURATION" --disable-multipart
      cell "get-$size-c$c-r$rep" get \
        --obj.size "$size" --objects "$(object_count "$size")" --concurrent "$c" --duration "$DURATION"
    done
  done

  for c in $MIXED_CONCURRENCY; do
    mixed_cell "mixed-$MIXED_SIZE-c$c-r$rep" "$c" "$DURATION"
  done
done

if [[ -n ${LONG_MIXED_DURATION:-} ]]; then
  c=${MIXED_CONCURRENCY##* }
  mixed_cell "longmixed-$MIXED_SIZE-c$c-r1" "$c" "$LONG_MIXED_DURATION"
fi

# Leave the engine running for inspection; stop both with:
#   docker compose -f bench/minio-compose.yml down; docker compose -f bench/seaweedfs-compose.yml down
log "done: $out"
