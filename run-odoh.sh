#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COREDNS_DIR="../coredns"
COREDNS_PID=""
DNSCRYPT_DIR="../dnscrypt-proxy"
DNSCRYPT_PID=""
SITES="${SITES:-sampled-100-of-2000-resolvable.csv}"
RUNS="${RUNS:-1}"
USE_COREDNS=false

# --- Parse arguments ---
for arg in "$@"; do
    case "$arg" in
        --coredns) USE_COREDNS=true ;;
    esac
done

INTERFACE=$(ip -o route show default | awk '{print $5; exit}')
echo "$INTERFACE"

# --- Create timestamped run directory ---
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$SCRIPT_DIR/runs/odoh-$TIMESTAMP"
mkdir -p "$RUN_DIR"
RESULTS_CSV="$RUN_DIR/results_har_odoh.csv"
BENCH_LOG="$RUN_DIR/benchmark-har-odoh.log"
SCRIPT_LOG="$RUN_DIR/run-odoh.log"

# Tee all script output to the run directory
exec > >(tee -a "$SCRIPT_LOG") 2>&1

echo "Run directory: $RUN_DIR"

cleanup() {
    echo ""
    echo "=== Cleaning up ==="

    # Stop coredns
    if $USE_COREDNS; then
        if [[ -n "$COREDNS_PID" ]] && kill -0 "$COREDNS_PID" 2>/dev/null; then
            echo "Stopping coredns (PID $COREDNS_PID)..."
            kill "$COREDNS_PID" 2>/dev/null || true
            wait "$COREDNS_PID" 2>/dev/null || true
        fi
    fi

    # Stop dnscrypt-proxy
    if [[ -n "$DNSCRYPT_PID" ]] && kill -0 "$DNSCRYPT_PID" 2>/dev/null; then
        echo "Stopping dnscrypt-proxy (PID $DNSCRYPT_PID)..."
        kill "$DNSCRYPT_PID" 2>/dev/null || true
        wait "$DNSCRYPT_PID" 2>/dev/null || true
    fi

    # Unset DNS

    ./unset-dns.sh "$INTERFACE"

    echo "=== Done ==="
}

trap cleanup EXIT

# --- Prompt for sudo upfront ---
echo "This script needs sudo to bind DNS to port 53."
sudo -v

# Set DNS System-wide
./set-dns.sh "$INTERFACE"

# CoreDNS Setup
if $USE_COREDNS; then
    echo "CoreDNS enabled: building and starting local CoreDNS server..."
    cd "$COREDNS_DIR"

    # ensure we're in the right branch
    git switch odoh

    # Build
    make

    # Run in background (run.sh uses exec, so we launch directly)
    ./coredns -conf=Corefile-ODOH.local & COREDNS_PID=$!
else
    echo "CoreDNS disabled: using public ODoH servers via dnscrypt-proxy."
fi

# DNSCrypt Proxy Setup

# --- Build and start dnscrypt-proxy ---
echo "Building and starting dnscrypt-proxy (ODoH)..."

# Kill any existing dnscrypt-proxy instances
if pgrep -x dnscrypt-proxy >/dev/null 2>&1; then
    echo "Stopping existing dnscrypt-proxy..."
    pkill -x dnscrypt-proxy || true
    sleep 2
    if pgrep -x dnscrypt-proxy >/dev/null 2>&1; then
        pkill -9 -x dnscrypt-proxy || true
    fi
fi

# Build
cd "$DNSCRYPT_DIR/dnscrypt-proxy"
go build -mod vendor
echo "Build complete."

# Run in background (run.sh uses exec, so we launch directly)
sudo ./dnscrypt-proxy -config dnscrypt-proxy-local-odoh.toml & DNSCRYPT_PID=$!
cd "$SCRIPT_DIR"

# Wait for it to be ready
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

# Verify DNS resolution through ODoH
echo "Verifying DNS resolution via ODoH..."
if dig google.com +short +timeout=10 >/dev/null 2>&1; then
    echo "DNS resolution OK."
else
    echo "WARNING: DNS resolution failed, proceeding anyway..."
fi

# --- Run benchmark ---
echo ""
echo "=== Running ODoH benchmark ==="
cd "$SCRIPT_DIR"
sudo apt install python3.12-venv -y

python3 -m venv venv
source venv/bin/activate
python -m pip install playwright

sleep 5

playwright install
playwright install-deps 

python benchmark-har.py odoh --randomize --runs="$RUNS" \
    --sites="$SITES" \
    --output="$RESULTS_CSV" \
    --log="$BENCH_LOG"

echo ""
echo "Benchmark complete. Results in $RUN_DIR"
