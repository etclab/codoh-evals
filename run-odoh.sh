#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DNSCRYPT_DIR="../dnscrypt-proxy"
DNSCRYPT_PID=""

cleanup() {
    echo ""
    echo "=== Cleaning up ==="

    # Stop dnscrypt-proxy
    if [[ -n "$DNSCRYPT_PID" ]] && kill -0 "$DNSCRYPT_PID" 2>/dev/null; then
        echo "Stopping dnscrypt-proxy (PID $DNSCRYPT_PID)..."
        kill "$DNSCRYPT_PID" 2>/dev/null || true
        wait "$DNSCRYPT_PID" 2>/dev/null || true
    fi

    echo "=== Done ==="
}

trap cleanup EXIT

# --- Prompt for sudo upfront ---
echo "This script needs sudo to bind DNS to port 53."
sudo -v

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
sudo ./dnscrypt-proxy -config dnscrypt-proxy.toml &
DNSCRYPT_PID=$!
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

python benchmark-har.py odoh --randomize --sites=top-10k-resolvable.csv --runs=1

echo ""
echo "Benchmark complete. Results in results_har_odoh.csv"
