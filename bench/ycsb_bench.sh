#!/usr/bin/env bash
#
# YCSB-style benchmarks for RocksDB using db_bench.
#
# All RocksDB engine tuning (write_buffer_size, compression, bloom filters,
# block cache, etc.) lives in a RocksDB options file (INI format) loaded
# via db_bench's --options_file flag.
#
# Each workload's db_bench-specific parameters (benchmarks, readwritepercent,
# seek_nexts, etc.) live in separate .ini files under workloads/.
#
# A top-level bench.ini supplies shared db_bench parameters (num, key_size,
# value_size, threads, duration, db path, reporting, …).  Export DB_DIR to
# override the database directory from bench.ini. Export DB_BENCH to set the
# db_bench binary path; --db_bench_bin overrides DB_BENCH.
#
# Usage:
#   ./ycsb_bench.sh [OPTIONS]
#
# Options:
#   --bench_ini <path>        Shared benchmark config      (default: bench/bench.ini)
#   --options_file <path>     RocksDB options file (INI)   (default: bench/rocksdb_options.ini)
#   --workload_dir <path>     Directory with workload .ini files (default: bench/workloads)
#   --db_bench_bin <path>     Path to db_bench binary      (overrides env DB_BENCH)
#   --workloads <list>        Comma-separated workloads    (default: A,B,C,D,E,F)
#   --memory_limit <size>     cgroup memory cap (MemoryMax) for each db_bench run. Examples:
#                             8G, 512M, 2147483648 (bytes). Uses systemd-run(1) --user --scope.
#                             Requires a user systemd session (logind). Omit or use 0/none to disable.
#

set -euo pipefail

# ── Locate repo root relative to this script ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Defaults ─────────────────────────────────────────────────────────
BENCH_INI="$SCRIPT_DIR/bench.ini"
OPTIONS_FILE="$SCRIPT_DIR/rocksdb_options.ini"
WORKLOAD_DIR="$SCRIPT_DIR/workloads"
DB_BENCH_BIN=""
WORKLOADS="A,B,C,D,E,F"
MEMORY_LIMIT_ARG=""

# ── Parse script-level arguments ─────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bench_ini)     BENCH_INI="$2";     shift 2;;
    --options_file)  OPTIONS_FILE="$2";  shift 2;;
    --workload_dir)  WORKLOAD_DIR="$2";  shift 2;;
    --db_bench_bin)  DB_BENCH_BIN="$2";  shift 2;;
    --workloads)     WORKLOADS="$2";     shift 2;;
    --memory_limit)  MEMORY_LIMIT_ARG="$2"; shift 2;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# ── Parse --memory_limit to bytes (for systemd MemoryMax=) ───────────
# Accepts: plain integer = bytes; suffix K/M/G/T = binary multiples (1024^n).
# 0, none, empty = unlimited.
MEMORY_LIMIT_BYTES=""
parse_memory_limit_bytes() {
  local raw="${1//[[:space:]]/}"
  local upper="${raw^^}"
  if [[ -z "$raw" || "$upper" == "NONE" || "$raw" == "0" ]]; then
    echo ""
    return 0
  fi
  if [[ "$raw" =~ ^[0-9]+$ ]]; then
    echo "$raw"
    return 0
  fi
  if [[ "${raw^^}" =~ ^([0-9]+)([KMGTP])$ ]]; then
    local n="${BASH_REMATCH[1]}"
    local suf="${BASH_REMATCH[2]}"
    case "$suf" in
      K) echo $(( n * 1024 )) ;;
      M) echo $(( n * 1024 * 1024 )) ;;
      G) echo $(( n * 1024 * 1024 * 1024 )) ;;
      T) echo $(( n * 1024 * 1024 * 1024 * 1024 )) ;;
      P) echo $(( n * 1024 * 1024 * 1024 * 1024 * 1024 )) ;;
    esac
    return 0
  fi
  echo "Invalid --memory_limit: $1 (use e.g. 8G, 512M, or a byte count)" >&2
  return 1
}
if [[ -n "$MEMORY_LIMIT_ARG" ]]; then
  MEMORY_LIMIT_BYTES="$(parse_memory_limit_bytes "$MEMORY_LIMIT_ARG")" || exit 1
fi

# ── Read an .ini file into an associative array ─────────────────────
# Lines are key=value.  Comments (#) and blank lines are skipped.
declare -A BENCH_VARS
load_ini() {
  local file="$1"
  local -n target_map="$2"
  if [[ ! -f "$file" ]]; then
    echo "Config file not found: $file" >&2
    exit 1
  fi
  while IFS='=' read -r k v; do
    k="${k%%#*}"                    # strip trailing comment
    k="$(echo "$k" | xargs)"       # trim whitespace
    v="$(echo "$v" | xargs)"
    [[ -z "$k" || "$k" == \#* ]] && continue
    target_map["$k"]="$v"
  done < "$file"
}

# ── Load shared bench config ────────────────────────────────────────
load_ini "$BENCH_INI" BENCH_VARS

# Pull required values (with fallback defaults)
# DB_DIR (env) overrides bench.ini `db` when set.
DB_PATH="${DB_DIR:-${BENCH_VARS[db]:-/tmp/rocksdb_ycsb_bench}}"
NUM="${BENCH_VARS[num]:-1000000}"
KEY_SIZE="${BENCH_VARS[key_size]:-16}"
VALUE_SIZE="${BENCH_VARS[value_size]:-1024}"
THREADS="${BENCH_VARS[threads]:-16}"
DURATION="${BENCH_VARS[duration]:-60}"
HISTOGRAM="${BENCH_VARS[histogram]:-false}"
STATISTICS="${BENCH_VARS[statistics]:-false}"
COMPRESSION_TYPE="${BENCH_VARS[compression_type]:-none}"
REPORT_INTERVAL_SECONDS="${BENCH_VARS[report_interval_seconds]:-1}"
REPORT_DIR="${BENCH_VARS[report_dir]:-$SCRIPT_DIR/reports}"

# ── Resolve db_bench binary: --db_bench_bin, then env DB_BENCH, then search ──
if [[ -z "$DB_BENCH_BIN" && -n "${DB_BENCH:-}" ]]; then
  DB_BENCH_BIN="$DB_BENCH"
fi
if [[ -z "$DB_BENCH_BIN" ]]; then
  for candidate in \
      "$REPO_ROOT/build/db_bench" \
      "$REPO_ROOT/db_bench" \
      "$(command -v db_bench 2>/dev/null || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      DB_BENCH_BIN="$candidate"
      break
    fi
  done
fi
if [[ -z "$DB_BENCH_BIN" ]]; then
  echo "Error: cannot find db_bench binary. Set DB_BENCH or use --db_bench_bin." >&2
  exit 1
fi
if [[ ! -x "$DB_BENCH_BIN" ]]; then
  echo "Error: db_bench is not executable: $DB_BENCH_BIN" >&2
  exit 1
fi

echo "============================================================"
echo " RocksDB YCSB Benchmark Suite"
echo "============================================================"
echo " db_bench:       $DB_BENCH_BIN"
echo " bench.ini:      $BENCH_INI"
echo " options_file:   $OPTIONS_FILE"
echo " workload_dir:   $WORKLOAD_DIR"
echo " db_path:        $DB_PATH"
echo " num:            $NUM"
echo " key_size:       $KEY_SIZE"
echo " value_size:     $VALUE_SIZE"
echo " threads:        $THREADS"
echo " duration:       ${DURATION}s"
echo " compression:    $COMPRESSION_TYPE"
echo " workloads:      $WORKLOADS"
if [[ "$REPORT_INTERVAL_SECONDS" -gt 0 ]]; then
  echo " report_every:   ${REPORT_INTERVAL_SECONDS}s"
  echo " report_dir:     $REPORT_DIR"
else
  echo " report_every:   (disabled)"
fi
if [[ -n "$MEMORY_LIMIT_BYTES" ]]; then
  echo " memory_limit:   ${MEMORY_LIMIT_BYTES} bytes (cgroup MemoryMax via systemd-run --user --scope)"
else
  echo " memory_limit:   (none)"
fi
echo "============================================================"
echo ""

# ── Build the common flag array from bench.ini ──────────────────────
# These flags apply to every db_bench invocation.
COMMON_FLAGS=(
  --db="$DB_PATH"
  --options_file="$OPTIONS_FILE"
  --num="$NUM"
  --key_size="$KEY_SIZE"
  --value_size="$VALUE_SIZE"
  --compression_type="$COMPRESSION_TYPE"
  --show_table_properties=true
)
if [[ "$STATISTICS" == "true" ]]; then
  COMMON_FLAGS+=(--statistics)
fi
if [[ "$HISTOGRAM" == "true" ]]; then
  COMMON_FLAGS+=(--histogram)
fi

# ── Convert a workload .ini into db_bench --flag arguments ───────────
ini_to_flags() {
  local file="$1"
  local -a flags=()
  while IFS='=' read -r k v; do
    k="${k%%#*}"
    k="$(echo "$k" | xargs)"
    v="$(echo "$v" | xargs)"
    [[ -z "$k" || "$k" == \#* ]] && continue
    flags+=("--${k}=${v}")
  done < "$file"
  echo "${flags[@]}"
}

# ── Helper: run db_bench (optional cgroup memory cap via systemd) ─────
run_bench() {
  if [[ -n "$MEMORY_LIMIT_BYTES" ]]; then
    if ! command -v systemd-run >/dev/null 2>&1; then
      echo "Error: systemd-run not found; --memory_limit requires systemd (cgroups)." >&2
      exit 1
    fi
    # Transient scope under the user manager: inherits env/cwd; MemoryMax applies
    # to the cgroup (RSS + cache charged to the group, unlike RLIMIT_AS).
    echo ">>> systemd-run --user --scope -p MemoryMax=${MEMORY_LIMIT_BYTES} -- $DB_BENCH_BIN $*"
    systemd-run --user --scope \
      -p "MemoryMax=${MEMORY_LIMIT_BYTES}" \
      -- "$DB_BENCH_BIN" "$@"
  else
    echo ">>> $DB_BENCH_BIN $*"
    "$DB_BENCH_BIN" "$@"
  fi
  echo ""
}

# ── Phase 1: Load ────────────────────────────────────────────────────
load_db() {
  local load_ini="$WORKLOAD_DIR/load.ini"

  echo "============================================================"
  echo " LOAD PHASE: Populating database with $NUM records"
  echo "============================================================"
  rm -rf "$DB_PATH"
  mkdir -p "$DB_PATH"

  local wl_flags
  read -ra wl_flags <<< "$(ini_to_flags "$load_ini")"

  run_bench \
    "${COMMON_FLAGS[@]}" \
    "${wl_flags[@]}"
}

# ── Phase 2: Run a single workload ──────────────────────────────────
run_workload() {
  local label="$1"
  local conf_file="$2"
  local report_flags=()
  local report_file=""

  echo "============================================================"
  echo " WORKLOAD $label"
  echo "   config: $conf_file"
  echo "============================================================"

  local wl_flags
  read -ra wl_flags <<< "$(ini_to_flags "$conf_file")"

  if [[ "$REPORT_INTERVAL_SECONDS" -gt 0 ]]; then
    mkdir -p "$REPORT_DIR"
    report_file="$REPORT_DIR/workload_$(echo "$label" | tr '[:upper:]' '[:lower:]')_timeseries.csv"
    report_flags=(
      --report_interval_seconds="$REPORT_INTERVAL_SECONDS"
      --report_file="$report_file"
    )
    echo "   report_file: $report_file"
  fi

  run_bench \
    "${COMMON_FLAGS[@]}" \
    --threads="$THREADS" \
    --duration="$DURATION" \
    "${report_flags[@]}" \
    "${wl_flags[@]}"
}

# ── Main ─────────────────────────────────────────────────────────────
IFS=',' read -ra SELECTED_WORKLOADS <<< "$WORKLOADS"

load_db

for wl in "${SELECTED_WORKLOADS[@]}"; do
  wl="$(echo "$wl" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
  conf_file="$WORKLOAD_DIR/workload_${wl}.ini"
  if [[ ! -f "$conf_file" ]]; then
    echo "No config found for workload $wl: expected $conf_file" >&2
    exit 1
  fi
  run_workload "$(echo "$wl" | tr '[:lower:]' '[:upper:]')" "$conf_file"
done

echo "============================================================"
echo " All workloads completed."
echo "============================================================"
