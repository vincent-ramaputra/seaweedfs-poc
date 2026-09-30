#!/usr/bin/env bash
# Measure how much disk space and how many inodes one engine variant uses per
# byte stored, for each object size in STORAGE_SIZES.
#
#   bench/storage.sh <variant>
#
# For each size: reset the engine, upload a fixed set of objects (single-part,
# like the PUT cells), leave them in place, sync, and measure the drives.
# Space used by the empty engine (measured right after the reset) is
# subtracted, so the result is what the objects themselves cost.
#
# Results go to bench/results/$BENCH_RUN/<variant>/storage/ (one JSON file per
# size, plus warp's logs); summarize.py turns them into a table. Sizes that
# already have a result are skipped, like sweep.sh cells.
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

variant=${1:?usage: storage.sh <minio|sw-minio-like|sw-tuned>}
engine_of "$variant" >/dev/null

# Small sizes need many objects to show per-object overhead; large sizes are
# capped by STORAGE_MAX_BYTES so the upload stays short.
: "${STORAGE_SIZES:=4KiB 128KiB 1MiB 6MiB 16MiB}"
: "${STORAGE_OBJECTS:=20000}"
: "${STORAGE_MAX_BYTES:=$((1024 * 1024 * 1024))}"
: "${STORAGE_CONCURRENCY:=16}"
# run_warp passes this to --analyze.skip; nothing here is timed.
SKIP=0s

out="$BENCH_DIR/results/${BENCH_RUN:-$(date +%Y%m%d)}/$variant"
sdir="$out/storage"
mkdir -p "$sdir"

size_bytes() {
  local s=$1
  case "$s" in
    *KiB) echo $((${s%KiB} * 1024)) ;;
    *MiB) echo $((${s%MiB} * 1024 * 1024)) ;;
    *GiB) echo $((${s%GiB} * 1024 * 1024 * 1024)) ;;
    *) echo "$s" ;;
  esac
}

object_count() {
  local cap=$((STORAGE_MAX_BYTES / $(size_bytes "$1")))
  ((cap < 16)) && cap=16
  ((STORAGE_OBJECTS < cap)) && echo "$STORAGE_OBJECTS" || echo "$cap"
}

# Print one JSON array with, per drive: KiB allocated on disk (du), bytes of
# file content (apparent size) and inodes (files + directories). Runs in a
# container because the engines write files as root.
measure_drives() {
  local i=1 d mounts=() script=''
  while read -r d; do
    mounts+=(-v "$d:/m$i:ro")
    script+="echo $i \$(du -sk /m$i | cut -f1) \$(find /m$i -type f -exec stat -c %s {} + | awk '{s+=\$1} END {print s+0}') \$((\$(find /m$i | wc -l) - 1));"
    i=$((i + 1))
  done < <(drive_paths)
  sync
  docker run --rm "${mounts[@]}" --entrypoint sh "$SW_IMAGE" -c "$script" |
    jq -R -s 'split("\n") | map(select(length > 0) | split(" ") | map(tonumber)
      | {drive: .[0], used_bytes: (.[1] * 1024), apparent_bytes: .[2], inodes: .[3]})'
}

fs_types() {
  drive_paths | while read -r d; do df --output=fstype "$d" | tail -1; done | sort -u | paste -sd,
}

# In storage/, so a sweep's environment.txt and matrix.env in $out stay as they are.
record_environment "$variant" "$sdir" /dev/null
rm -f "$sdir/matrix.env"
log "variant $variant, storage test, results in $sdir"

for size in $STORAGE_SIZES; do
  name="storage-$size"
  if [[ -f $sdir/$name.json ]]; then
    log "skip $name (already done)"
    continue
  fi
  n=$(object_count "$size")
  reset_engine "$variant"
  empty=$(measure_drives)
  # warp get uploads exactly --objects objects before its (here, token) GET
  # phase; --noclear keeps them afterwards instead of deleting them.
  run_warp "$variant" "$sdir" "$name" get \
    --obj.size "$size" --objects "$n" --concurrent "$STORAGE_CONCURRENCY" \
    --duration 1s --disable-multipart --noclear
  # Count what was actually stored, from warp's per-request record.
  read -r stored logical < <(zstd -dc "$sdir/$name.csv.zst" |
    awk -F'\t' '$3 == "PUT" && $9 == "" {n++; b += $6} END {print n+0, b+0}')
  # warp splits the objects evenly across clients, rounding up, so a few extra is normal.
  ((stored >= n)) || log "WARNING: $name: only $stored of $n uploads succeeded (see $name.log)"
  full=$(measure_drives)
  jq -n --arg variant "$variant" --arg size "$size" --argjson size_bytes "$(size_bytes "$size")" \
    --argjson objects "$stored" --argjson logical_bytes "$logical" --arg fs "$(fs_types)" \
    --argjson empty "$empty" --argjson full "$full" \
    '{variant: $variant, size: $size, size_bytes: $size_bytes, objects: $objects,
      logical_bytes: $logical_bytes, filesystem: $fs, empty: $empty, full: $full}' >"$sdir/$name.json"
  log "$name: $stored objects measured"
done

# Leave the engine running for inspection; stop both with:
#   docker compose -f bench/minio-compose.yml down; docker compose -f bench/seaweedfs-compose.yml down
log "done: $sdir"
