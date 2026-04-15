#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# run-codoh.sh — CoDOH (Cached Oblivious DNS over HTTPS) benchmark runner
#
# CoDOH requires a 3-process stack launched in order:
#   1. Enclave   — HPKE keypair, cache, Q_E decryption (simulation mode, no SGX)
#   2. Target    — ODoH resolution, cache-insert encryption, Ed25519 signing
#   3. Proxy     — Client-facing relay, enclave IPC, target forwarding
#
# Then dnscrypt-proxy sits in front as the local DNS listener on port 53.
#
# This follows the same pattern as coredns/benchmark/configs/4-codoh-nosgx.sh
# (enclave-sim self-provisions, no attestation, Corefile.target-nosgx).
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COREDNS_DIR="../coredns"
DNSCRYPT_DIR="../dnscrypt-proxy"

ENCLAVE_PID=""
COREDNS_TARGET_PID=""
COREDNS_PROXY_PID=""
DNSCRYPT_PID=""

SITES="${SITES:-sampled-100-of-2000-resolvable.csv}"
RUNS="${RUNS:-1}"

# Enclave settings (simulation mode — no SGX hardware required)
ENCLAVE_SOCKET="/tmp/codoh-enclave.sock"

# TLS certs from the .signing directory in the coredns repo
SIGNING_DIR="$COREDNS_DIR/.signing"

INTERFACE=$(ip -o route show default | awk '{print $5; exit}')
echo "Network interface: $INTERFACE"

# --- Create timestamped run directory ---
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$SCRIPT_DIR/runs/codoh-$TIMESTAMP"
mkdir -p "$RUN_DIR"
RESULTS_CSV="$RUN_DIR/results_har_codoh.csv"
BENCH_LOG="$RUN_DIR/benchmark-har-codoh.log"
SCRIPT_LOG="$RUN_DIR/run-codoh.log"

# Tee all script output to the run directory
exec > >(tee -a "$SCRIPT_LOG") 2>&1

echo "Run directory: $RUN_DIR"

cleanup() {
    echo ""
    echo "=== Cleaning up ==="

    # Stop coredns proxy
    if [[ -n "$COREDNS_PROXY_PID" ]] && kill -0 "$COREDNS_PROXY_PID" 2>/dev/null; then
        echo "Stopping coredns proxy (PID $COREDNS_PROXY_PID)..."
        kill "$COREDNS_PROXY_PID" 2>/dev/null || true
        wait "$COREDNS_PROXY_PID" 2>/dev/null || true
    fi

    # Stop coredns target
    if [[ -n "$COREDNS_TARGET_PID" ]] && kill -0 "$COREDNS_TARGET_PID" 2>/dev/null; then
        echo "Stopping coredns target (PID $COREDNS_TARGET_PID)..."
        kill "$COREDNS_TARGET_PID" 2>/dev/null || true
        wait "$COREDNS_TARGET_PID" 2>/dev/null || true
    fi

    # Stop enclave
    if [[ -n "$ENCLAVE_PID" ]] && kill -0 "$ENCLAVE_PID" 2>/dev/null; then
        echo "Stopping enclave (PID $ENCLAVE_PID)..."
        kill "$ENCLAVE_PID" 2>/dev/null || true
        wait "$ENCLAVE_PID" 2>/dev/null || true
    fi

    # Stop dnscrypt-proxy
    if [[ -n "$DNSCRYPT_PID" ]] && kill -0 "$DNSCRYPT_PID" 2>/dev/null; then
        echo "Stopping dnscrypt-proxy (PID $DNSCRYPT_PID)..."
        kill "$DNSCRYPT_PID" 2>/dev/null || true
        wait "$DNSCRYPT_PID" 2>/dev/null || true
    fi

    # Clean up enclave socket
    rm -f "$ENCLAVE_SOCKET"

    # Unset DNS
    cd "$SCRIPT_DIR"
    ./unset-dns.sh "$INTERFACE"

    echo "=== Done ==="
}

trap cleanup EXIT

# --- Prompt for sudo upfront ---
echo "This script needs sudo to bind DNS to port 53."
sudo -v

# =============================================================================
# Step 0: Build CoreDNS (codoh-design-v2 branch)
# =============================================================================
echo ""
echo "=== Building CoreDNS (codoh-design-v2) ==="
cd "$COREDNS_DIR"
git switch codoh-design-v2

# Build coredns-test binary (same as benchmark/run-all.sh)
go build -o coredns-test .

# Build enclave-sim (plain Go binary, no EGo/SGX needed)
go build -o enclave-sim ./enclave/cmd

echo "CoreDNS + enclave-sim build complete."

# =============================================================================
# Step 1: Start Enclave (simulation mode — no SGX hardware)
#
# enclave-sim self-provisions immediately (no attestation server, no
# provisioning flow). It creates the IPC socket at $ENCLAVE_SOCKET
# without waiting for a target to call /provision.
# =============================================================================
echo ""
echo "=== Starting Enclave (simulation mode, no SGX) ==="

# Clean up stale socket
rm -f "$ENCLAVE_SOCKET"

CODOH_CACHE_SIZE=10000 \
CODOH_BATCH_SIZE=10 \
CODOH_BATCH_COMMIT_PROB=1.0 \
CODOH_WARMUP_THRESHOLD=5 \
CODOH_REPLAY_DELTA_SECS=30 \
./enclave-sim \
    > "$RUN_DIR/enclave.log" 2>&1 &
ENCLAVE_PID=$!
echo "Enclave started (PID $ENCLAVE_PID)"

# Wait for the enclave IPC socket to appear
echo "Waiting for enclave IPC socket..."
for i in $(seq 1 30); do
    if [[ -S "$ENCLAVE_SOCKET" ]]; then
        echo "Enclave IPC socket ready."
        break
    fi
    if ! kill -0 "$ENCLAVE_PID" 2>/dev/null; then
        echo "ERROR: Enclave exited unexpectedly. Check $RUN_DIR/enclave.log"
        ENCLAVE_PID=""
        exit 1
    fi
    sleep 1
done

# =============================================================================
# Step 2: Start CoreDNS Target (codohtarget plugin, port 8443)
#
# Uses Corefile.target-nosgx (no enclave_url directive — simulation mode
# skips the attestation/provisioning flow).
# =============================================================================
echo ""
echo "=== Starting CoreDNS Target ==="

# Write the target Corefile using .signing certs (based on Corefile.target-nosgx)
cat > "$RUN_DIR/Corefile.target-nosgx" <<EOF
.:25356 {
    codohtarget {
        port 8443
        tls_cert $SIGNING_DIR/target.pem
        tls_key $SIGNING_DIR/target-key.pem
        upstream 1.1.1.1:53
        signing_key /tmp/target-signing.pem
        log_queries true
    }

    errors
    log
}
EOF

CODOH_COVER_COUNT=3 \
CODOH_COVER_DOMAIN_FILE="$COREDNS_DIR/benchmark/top-1m.csv" \
CODOH_PROXY_CALLBACK_URL=https://127.0.0.1:8080 \
CODOH_COVER_RESOLVER=1.1.1.1:53 \
CODOH_COVER_TIMEOUT_MS=2000 \
./coredns-test -conf "$RUN_DIR/Corefile.target-nosgx" \
    > "$RUN_DIR/coredns-target.log" 2>&1 &
COREDNS_TARGET_PID=$!
echo "CoreDNS Target started (PID $COREDNS_TARGET_PID), port 8443"

sleep 3

if ! kill -0 "$COREDNS_TARGET_PID" 2>/dev/null; then
    echo "ERROR: CoreDNS target exited unexpectedly. Check $RUN_DIR/coredns-target.log"
    COREDNS_TARGET_PID=""
    exit 1
fi
echo "CoreDNS Target is running."

# =============================================================================
# Step 3: Start CoreDNS Proxy (codohproxy plugin, port 8080)
# =============================================================================
echo ""
echo "=== Starting CoreDNS Proxy ==="

# Write the proxy Corefile using .signing certs (based on Corefile.proxy)
cat > "$RUN_DIR/Corefile.proxy" <<EOF
.:25357 {
    codohproxy {
        target https://127.0.0.1:8443/dns-query
        port 8080
        tls_cert $SIGNING_DIR/proxy.pem
        tls_key $SIGNING_DIR/proxy-key.pem
        insecure_skip_verify true

        enclave_enabled
        enclave_socket $ENCLAVE_SOCKET
        enclave_bypass_on_failure true
    }

    errors
    log
}
EOF

./coredns-test -conf "$RUN_DIR/Corefile.proxy" \
    > "$RUN_DIR/coredns-proxy.log" 2>&1 &
COREDNS_PROXY_PID=$!
echo "CoreDNS Proxy started (PID $COREDNS_PROXY_PID), port 8080"

sleep 3

if ! kill -0 "$COREDNS_PROXY_PID" 2>/dev/null; then
    echo "ERROR: CoreDNS proxy exited unexpectedly. Check $RUN_DIR/coredns-proxy.log"
    COREDNS_PROXY_PID=""
    exit 1
fi
echo "CoreDNS Proxy is running."

# =============================================================================
# Step 4: Build and start dnscrypt-proxy (codoh-design-v2 branch)
# =============================================================================
echo ""
echo "=== Building and starting dnscrypt-proxy (CoDOH) ==="

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
git switch codoh-design-v2
go build -mod vendor
echo "dnscrypt-proxy build complete."

# Use the CoDOH config file from the dnscrypt-proxy repo.
DNSCRYPT_CONF="$RUN_DIR/dnscrypt-proxy-codoh.toml"
cat dnscrypt-proxy-codoh.toml > "$DNSCRYPT_CONF"
echo "Using dnscrypt-proxy config: $DNSCRYPT_CONF"

sudo ./dnscrypt-proxy -config "$DNSCRYPT_CONF" & DNSCRYPT_PID=$!
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

# Verify DNS resolution through CoDOH
echo "Verifying DNS resolution via CoDOH..."
if dig google.com +short +timeout=10 >/dev/null 2>&1; then
    echo "DNS resolution OK."
else
    echo "WARNING: DNS resolution failed, proceeding anyway..."
fi

# =============================================================================
# Step 5: Run benchmark
# =============================================================================
echo ""
echo "=== Running CoDOH benchmark ==="
cd "$SCRIPT_DIR"
sudo apt install python3.12-venv -y

python3 -m venv venv
source venv/bin/activate
python -m pip install playwright

sleep 5

playwright install
playwright install-deps 

# Set DNS System-wide (only after everything has been installed)
./set-dns.sh "$INTERFACE"

python benchmark-har.py codoh --randomize --runs="$RUNS" \
    --sites="$SITES" \
    --output="$RESULTS_CSV" \
    --log="$BENCH_LOG"

echo ""
echo "Benchmark complete. Results in $RUN_DIR"
