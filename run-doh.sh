#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DNSCRYPT_DIR="../dnscrypt-proxy"
DNSCRYPT_LISTEN="127.0.0.1:5300"
DNSCRYPT_PID=""
SITES="${SITES:-sampled-100-of-2000-resolvable.csv}"
RUNS="${RUNS:-1}"
IFACE="ens4059f0np0" # while running on shs3, change if needed
# IFACE="enp193s0f0np0" # while running on shs4, change if needed

cleanup() {
    echo ""
    echo "=== Cleaning up ==="

    # Stop dnscrypt-proxy
    if [[ -n "$DNSCRYPT_PID" ]] && kill -0 "$DNSCRYPT_PID" 2>/dev/null; then
        echo "Stopping dnscrypt-proxy (PID $DNSCRYPT_PID)..."
        kill "$DNSCRYPT_PID" 2>/dev/null || true
        wait "$DNSCRYPT_PID" 2>/dev/null || true
    fi

    # Revert DNS and re-enable resolved cache
    "$SCRIPT_DIR/unset-dns.sh" "$IFACE"

    echo "=== Done ==="
}

trap cleanup EXIT

# --- Prompt for sudo upfront ---
echo "This script needs sudo to change DNS settings."
sudo -v

# --- Build and start dnscrypt-proxy (DoH mode) ---
echo "Building and starting dnscrypt-proxy (DoH) on $DNSCRYPT_LISTEN..."

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

# Run in background with the DoH-specific config
./dnscrypt-proxy -config dnscrypt-proxy-doh.toml &
DNSCRYPT_PID=$!
cd "$SCRIPT_DIR"

# Wait for it to be ready
echo "Waiting for dnscrypt-proxy to start..."
for i in $(seq 1 30); do
    if dig @127.0.0.1 -p 5300 google.com +short +timeout=2 >/dev/null 2>&1; then
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

# --- Override system DNS (also disables resolved cache) ---
"$SCRIPT_DIR/set-dns.sh" "$IFACE" "$DNSCRYPT_LISTEN"

# Verify DNS resolution through DoH
echo "Verifying DNS resolution via DoH..."
if dig google.com +short +timeout=10 >/dev/null 2>&1; then
    echo "DNS resolution OK."
else
    echo "WARNING: DNS resolution failed, proceeding anyway..."
fi

# --- Run benchmark ---
echo ""
echo "=== Running DoH benchmark ==="
cd "$SCRIPT_DIR"
sudo apt install python3.12-venv -y

python3 -m venv venv
source venv/bin/activate
python -m pip install playwright

sleep 5

playwright install
playwright install-deps 

python benchmark-har.py doh --randomize --runs="$RUNS" \
    --sites="$SITES" 

echo ""
echo "Benchmark complete. Results in results_har_doh.csv"
