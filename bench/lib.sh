# Shared helpers for the benchmark scripts. Source this file, don't run it.

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$BENCH_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$BENCH_DIR/.env"
  set +a
fi

# Defaults must match the compose files.
: "${DRIVE1:=./drives/d1}" "${DRIVE2:=./drives/d2}" "${DRIVE3:=./drives/d3}" "${DRIVE4:=./drives/d4}"
: "${MINIO_PORT:=9100}" "${SW_S3_PORT:=8433}"
: "${SW_IMAGE:=chrislusf/seaweedfs:4.40}" "${WARP_IMAGE:=minio/warp:v1.3.1}"
: "${BENCH_ACCESS_KEY:=benchadmin}" "${BENCH_SECRET_KEY:=benchadmin-secret}"
: "${BENCH_DROP_CACHES:=1}"
export DRIVE1 DRIVE2 DRIVE3 DRIVE4 MINIO_PORT SW_S3_PORT SW_IMAGE WARP_IMAGE BENCH_ACCESS_KEY BENCH_SECRET_KEY

BENCH_BUCKET=warp-benchmark-bucket
SW_MASTER=127.0.0.1:9433
SW_FILER=127.0.0.1:8988

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# minio -> minio, sw-* -> seaweedfs
engine_of() {
  case "$1" in
    minio) echo minio ;;
    sw-r001 | sw-r001-fsync) echo seaweedfs ;;
    *) die "unknown variant '$1' (expected minio, sw-r001, sw-r001-fsync)" ;;
  esac
}

compose() {
  local engine=$1
  shift
  docker compose -f "$BENCH_DIR/$engine-compose.yml" "$@"
}

endpoint() {
  case "$1" in
    minio) echo "127.0.0.1:$MINIO_PORT" ;;
    seaweedfs) echo "127.0.0.1:$SW_S3_PORT" ;;
  esac
}

drive_paths() {
  local v p
  for v in DRIVE1 DRIVE2 DRIVE3 DRIVE4; do
    p=${!v}
    [[ $p == /* ]] || p="$BENCH_DIR/${p#./}"
    echo "$p"
  done
}

write_s3_config() {
  cat >"$BENCH_DIR/s3.generated.json" <<EOF
{
  "identities": [
    {
      "name": "bench",
      "credentials": [{"accessKey": "$BENCH_ACCESS_KEY", "secretKey": "$BENCH_SECRET_KEY"}],
      "actions": ["Admin", "Read", "List", "Tagging", "Write"]
    }
  ]
}
EOF
}

# Empty every drive. Runs in a container because the engines write files as root.
# For seaweedfs, also create the directories its services expect.
wipe_drives() {
  local engine=$1 i=1 d
  local mounts=() script='rm -rf /wipe*/* /wipe*/.[!.]* 2>/dev/null; true'
  while read -r d; do
    mkdir -p "$d"
    mounts+=(-v "$d:/wipe$i")
    i=$((i + 1))
  done < <(drive_paths)
  if [[ $engine == seaweedfs ]]; then
    # The wipe runs as root, but SeaweedFS's entrypoint drops to a non-root user.
    script+='; mkdir -p /wipe1/sw-master /wipe1/sw-filer /wipe1/sw-volume /wipe2/sw-volume /wipe3/sw-volume /wipe4/sw-volume'
    script+='; chmod 777 /wipe1/sw-master /wipe1/sw-filer /wipe*/sw-volume'
  fi
  docker run --rm "${mounts[@]}" --entrypoint sh "$SW_IMAGE" -c "$script"
}

wait_ready() {
  local engine=$1 tries=120 url
  case "$engine" in
    minio) url="http://$(endpoint minio)/minio/health/live" ;;
    # Unauthenticated requests get 403 once the S3 API is serving.
    seaweedfs) url="http://$(endpoint seaweedfs)/" ;;
  esac
  until [[ $(curl -s -o /dev/null -w '%{http_code}' "$url") =~ ^(200|403)$ ]]; do
    ((tries--)) || die "$engine did not become ready"
    sleep 1
  done
  if [[ $engine == seaweedfs ]]; then
    tries=60
    until [[ $(sw_volume_server_count) -ge 4 ]]; do
      ((tries--)) || die "seaweedfs: fewer than 4 volume servers registered"
      sleep 1
    done
  fi
}

sw_volume_server_count() {
  curl -s "http://$SW_MASTER/dir/status" |
    jq '[.Topology.DataCenters[]?.Racks[]?.DataNodes[]?] | length' 2>/dev/null || echo 0
}

# Run weed shell commands (passed as one string, newline separated).
sw_shell() {
  compose seaweedfs exec -T master weed shell -master="$SW_MASTER" -filer="$SW_FILER" <<<"$1"
}

# Stop both engines, wipe the drives, and start the variant fresh.
reset_engine() {
  local variant=$1 engine
  engine=$(engine_of "$variant")
  log "resetting $variant"
  compose minio down --remove-orphans >/dev/null 2>&1 || true
  compose seaweedfs down --remove-orphans >/dev/null 2>&1 || true
  wipe_drives "$engine"
  [[ $engine == seaweedfs ]] && write_s3_config
  compose "$engine" --progress quiet up -d >/dev/null
  wait_ready "$engine"
  if [[ $variant == sw-r001-fsync ]]; then
    sw_shell "fs.configure -locationPrefix=/buckets/ -fsync=true -apply" >/dev/null
  fi
  drop_caches
}

drop_caches() {
  sync
  [[ $BENCH_DROP_CACHES == 1 ]] || return 0
  if sudo -n true 2>/dev/null; then
    echo 3 | sudo -n tee /proc/sys/vm/drop_caches >/dev/null
  elif [[ -z ${_DROP_CACHES_WARNED:-} ]]; then
    log "WARNING: passwordless sudo unavailable; page cache is NOT dropped between runs"
    _DROP_CACHES_WARNED=1
  fi
}

# run_warp <variant> <outdir> <name> <warp op> [warp flags...]
# Writes <name>.log, <name>.cmd and warp's benchdata into outdir. --full keeps
# every request so summarize.py can compute exact p99.9 and TTFB after
# dropping the warm-up.
run_warp() {
  local variant=$1 outdir=$2 name=$3 op=$4
  shift 4
  local engine cmd
  engine=$(engine_of "$variant")
  cmd=(docker run --rm --network host -v "$outdir:/out" "$WARP_IMAGE" "$op"
    --host "$(endpoint "$engine")"
    --access-key "$BENCH_ACCESS_KEY" --secret-key "$BENCH_SECRET_KEY"
    --bucket "$BENCH_BUCKET"
    --benchdata "/out/$name"
    --full
    --analyze.skip "$SKIP"
    # "--noclear"
    "$@")
  printf '%q ' "${cmd[@]}" | sed "s/$BENCH_SECRET_KEY/***/" >"$outdir/$name.cmd"
  log "$variant: $op $name"
  "${cmd[@]}" >"$outdir/$name.log" 2>&1 || log "WARNING: warp exited non-zero for $name (see $name.log)"
}

_METRIC_PIDS=()

# Block devices backing the drive paths (e.g. "sdb sdc"), or empty if unknown.
drive_devices() {
  drive_paths | while read -r d; do df --output=source "$d" | tail -1; done |
    grep '^/dev/' | sed 's#^/dev/##' | sort -u | tr '\n' ' '
}

# Sample the bench containers' stats and the drives' disk stats every 5s
# until stop_metrics.
start_metrics() {
  local prefix=$1 devices
  devices=$(drive_devices)
  (while :; do
    # shellcheck disable=SC2046 # one argument per container id
    docker stats --no-stream --format "$(date +%s) {{json .}}" $(docker ps -q --filter name=^bench-)
    sleep 5
  done) >"$prefix.docker-stats.jsonl" 2>/dev/null &
  _METRIC_PIDS+=($!)
  # shellcheck disable=SC2086 # empty = all devices
  iostat -dxmty $devices 5 >"$prefix.iostat.txt" 2>/dev/null &
  _METRIC_PIDS+=($!)
}

stop_metrics() {
  local pid
  for pid in "${_METRIC_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  _METRIC_PIDS=()
}

# Save everything needed to reproduce and interpret a run.
record_environment() {
  local variant=$1 outdir=$2 matrix=$3 engine
  engine=$(engine_of "$variant")
  {
    echo "variant: $variant"
    echo "date: $(date -Is)"
    echo "git: $(git -C "$BENCH_DIR" rev-parse --short HEAD 2>/dev/null)"
    echo "warp image: $WARP_IMAGE"
    if [[ $engine == minio ]]; then
      echo "engine: $(compose minio exec -T minio minio --version 2>/dev/null | head -1)"
    else
      echo "engine: $(compose seaweedfs exec -T master weed version 2>/dev/null | head -1)"
    fi
    echo "cpus: $(nproc)"
    free -h
    lsblk -d -o NAME,SIZE,ROTA,MODEL 2>/dev/null | grep -v '^loop'
    echo "--- drives"
    drive_paths | while read -r d; do df -h "$d" | tail -1; done
  } >"$outdir/environment.txt"
  cp "$matrix" "$outdir/matrix.env"
  [[ -f $BENCH_DIR/.env ]] && sed 's/^BENCH_SECRET_KEY=.*/BENCH_SECRET_KEY=***/' "$BENCH_DIR/.env" >"$outdir/bench.env"
  return 0
}
