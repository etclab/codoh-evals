#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# run-doh-parallel.sh — Run DoH benchmarks for multiple CSV files in parallel.
#
# Usage:
#   RUNS=1 ./run-doh-parallel.sh [--coredns] [--jobs N]
#
# By default runs all 10 CSV chunks (top-1k.csv through top-9001-10k.csv).
# Override with:  SITE_FILES="top-1k.csv top-1001-2k.csv" ./run-doh-parallel.sh
#
# --jobs N   Max parallel benchmark processes (default: 10, i.e. all at once)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COREDNS_DIR="../coredns"
COREDNS_PID=""
DNSCRYPT_DIR="../dnscrypt-proxy"
DNSCRYPT_PID=""
RUNS="${RUNS:-1}"
USE_COREDNS=false
MAX_JOBS=10

# All 10 CSV chunks by default
DEFAULT_SITE_FILES=(
    top-1k.csv
    top-1001-2k.csv
    top-2001-3k.csv
    top-3001-4k.csv
    top-4001-5k.csv
    top-5001-6k.csv
    top-6001-7k.csv
    top-7001-8k.csv
    top-8001-9k.csv
    top-9001-10k.csv
)

# --- Parse arguments ---
for arg in "$@"; do
    case "$arg" in
        --coredns) USE_COREDNS=true ;;
        --jobs=*) MAX_JOBS="${arg#--jobs=}" ;;
        --jobs) ;; # handled below with next arg
    esac
done
# Handle --jobs N (space-separated)
while [[ $# -gt 0 ]]; do
    case "$1" in
        --jobs) MAX_JOBS="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# Allow overriding the file list via env var
if [[ -n "${SITE_FILES:-}" ]]; then
    read -ra CSV_FILES <<< "$SITE_FILES"
else
    CSV_FILES=("${DEFAULT_SITE_FILES[@]}")
fi

INTERFACE=$(ip -o route show default | awk '{print $5; exit}')
echo "Interface: $INTERFACE"
echo "Parallel jobs: $MAX_JOBS"
echo "CSV files: ${CSV_FILES[*]}"
echo "Runs per site: $RUNS"
echo ""

# --- Create a top-level run directory for this batch ---
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
if $USE_COREDNS; then
    BATCH_DIR="$SCRIPT_DIR/runs/doh-coredns-parallel-$TIMESTAMP"
else
    BATCH_DIR="$SCRIPT_DIR/runs/doh-parallel-$TIMESTAMP"
fi
mkdir -p "$BATCH_DIR"
SCRIPT_LOG="$BATCH_DIR/run-doh-parallel.log"

exec > >(tee -a "$SCRIPT_LOG") 2>&1
echo "Batch directory: $BATCH_DIR"

# =============================================================================
# CLEANUP — runs once on exit, tears down DNS + background daemons
# =============================================================================
cleanup() {
    echo ""
    echo "=== Cleaning up ==="

    if $USE_COREDNS; then
        if [[ -n "$COREDNS_PID" ]] && kill -0 "$COREDNS_PID" 2>/dev/null; then
            echo "Stopping coredns (PID $COREDNS_PID)..."
            kill "$COREDNS_PID" 2>/dev/null || true
            wait "$COREDNS_PID" 2>/dev/null || true
        fi
    fi

    if [[ -n "$DNSCRYPT_PID" ]] && kill -0 "$DNSCRYPT_PID" 2>/dev/null; then
        echo "Stopping dnscrypt-proxy (PID $DNSCRYPT_PID)..."
        kill "$DNSCRYPT_PID" 2>/dev/null || true
        wait "$DNSCRYPT_PID" 2>/dev/null || true
    fi

    cd "$SCRIPT_DIR"
    ./unset-dns.sh "$INTERFACE"

    echo "=== Done ==="
}
trap cleanup EXIT

# =============================================================================
# PHASE 1: One-time setup (DNS infra, builds, venv)
# =============================================================================
echo "=== Phase 1: One-time setup ==="

# --- Prompt for sudo upfront ---
echo "This script needs sudo to bind DNS to port 53."
sudo -v

# --- CoreDNS (optional) ---
if $USE_COREDNS; then
    echo "Building and starting CoreDNS..."
    cd "$COREDNS_DIR"
    git switch odoh
    make
    ./coredns -conf=Corefile-DOH.local & COREDNS_PID=$!
fi

# --- dnscrypt-proxy ---
echo "Building and starting dnscrypt-proxy (DoH)..."

if pgrep -x dnscrypt-proxy >/dev/null 2>&1; then
    echo "Stopping existing dnscrypt-proxy..."
    pkill -x dnscrypt-proxy || true
    sleep 2
    if pgrep -x dnscrypt-proxy >/dev/null 2>&1; then
        pkill -9 -x dnscrypt-proxy || true
    fi
fi

cd "$DNSCRYPT_DIR/dnscrypt-proxy"
git switch master
go build -mod vendor
echo "Build complete."

if $USE_COREDNS; then
    DNSCRYPT_CONF="dnscrypt-proxy-local-doh.toml"
else
    DNSCRYPT_CONF="dnscrypt-proxy-doh.toml"
fi
echo "Using dnscrypt-proxy config: $DNSCRYPT_CONF"
sudo ./dnscrypt-proxy -config "$DNSCRYPT_CONF" & DNSCRYPT_PID=$!
cd "$SCRIPT_DIR"

echo "Waiting for dnscrypt-proxy to start..."
for i in $(seq 1 30); do
    if dig google.com +short +timeout=2 >/dev/null 2>&1; then
        echo "dnscrypt-proxy is ready."
        break
    fi
    if ! kill -0 "$DNSCRYPT_PID" 2>/dev/null; then
        echo "ERROR: dnscrypt-proxy exited unexpectedly."
        DNSCRYPT_PID=""
        exit 1
    fi
    sleep 1
done

echo "Verifying DNS resolution via DoH..."
if dig google.com +short +timeout=10 >/dev/null 2>&1; then
    echo "DNS resolution OK."
else
    echo "WARNING: DNS resolution failed, proceeding anyway..."
fi

# --- Python / Playwright setup ---
cd "$SCRIPT_DIR"
sudo apt install python3.12-venv -y
python3 -m venv venv
source venv/bin/activate
python -m pip install playwright
sleep 5
playwright install
playwright install-deps

# --- Set system DNS to point through dnscrypt-proxy ---
./set-dns.sh "$INTERFACE"

echo ""
echo "=== Phase 1 complete. DNS is configured, ready to benchmark. ==="
echo ""

# =============================================================================
# PHASE 2: Run benchmarks in parallel
# =============================================================================
echo "=== Phase 2: Running ${#CSV_FILES[@]} benchmarks (max $MAX_JOBS parallel) ==="

PIDS=()
SITE_LABELS=()

for csv_file in "${CSV_FILES[@]}"; do
    label="${csv_file%.csv}"

    # Per-file output directory
    RUN_DIR="$BATCH_DIR/$label"
    mkdir -p "$RUN_DIR"

    RESULTS_CSV="$RUN_DIR/results_har_doh.csv"
    BENCH_LOG="$RUN_DIR/benchmark-har-doh.log"

    echo "Starting benchmark: $csv_file -> $RUN_DIR"

    python benchmark-har.py doh --randomize --runs="$RUNS" \
        --sites="$csv_file" \
        --output="$RESULTS_CSV" \
        --log="$BENCH_LOG" \
        > "$RUN_DIR/stdout.log" 2>&1 &

    PIDS+=($!)
    SITE_LABELS+=("$csv_file")

    # Throttle: if we've hit MAX_JOBS, wait for one to finish before launching more
    if (( ${#PIDS[@]} >= MAX_JOBS )); then
        wait -n || true
        # Remove finished PIDs from the array
        LIVE_PIDS=()
        LIVE_LABELS=()
        for idx in "${!PIDS[@]}"; do
            if kill -0 "${PIDS[$idx]}" 2>/dev/null; then
                LIVE_PIDS+=("${PIDS[$idx]}")
                LIVE_LABELS+=("${SITE_LABELS[$idx]}")
            fi
        done
        PIDS=("${LIVE_PIDS[@]}")
        SITE_LABELS=("${LIVE_LABELS[@]}")
    fi
done

# --- Wait for all remaining benchmarks to finish ---
echo ""
echo "Waiting for all benchmarks to complete..."
FAILURES=0
for idx in "${!PIDS[@]}"; do
    if wait "${PIDS[$idx]}"; then
        echo "  DONE: ${SITE_LABELS[$idx]}"
    else
        echo "  FAIL: ${SITE_LABELS[$idx]} (exit code $?)"
        ((FAILURES++))
    fi
done

echo ""
echo "=== All benchmarks finished. ==="
echo "  Total:    ${#CSV_FILES[@]}"
echo "  Failed:   $FAILURES"
echo "  Results:  $BATCH_DIR"
echo ""

# List result files
for csv_file in "${CSV_FILES[@]}"; do
    label="${csv_file%.csv}"
    result="$BATCH_DIR/$label/results_har_doh.csv"
    if [[ -f "$result" ]]; then
        rows=$(wc -l < "$result")
        echo "  $label: $rows rows"
    else
        echo "  $label: NO OUTPUT"
    fi
done
