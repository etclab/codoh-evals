#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DNSCRYPT_BIN="/home/apoudel01/downloads/project-codoh/dnscrypt-proxy/dnscrypt-proxy/dnscrypt-proxy"
DNSCRYPT_CONFIG="/home/apoudel01/downloads/project-codoh/dnscrypt-proxy/dnscrypt-proxy/dnscrypt-proxy.toml"
DNSCRYPT_LISTEN="127.0.0.1:5300"
DNSCRYPT_PID=""
IFACE="enp113s0f0np0"
ORIGINAL_DNS=""
RESOLVED_CONF_DROP="/etc/systemd/resolved.conf.d/no-cache.conf"

cleanup() {
    echo ""
    echo "=== Cleaning up ==="

    # Stop dnscrypt-proxy
    if [[ -n "$DNSCRYPT_PID" ]] && kill -0 "$DNSCRYPT_PID" 2>/dev/null; then
        echo "Stopping dnscrypt-proxy (PID $DNSCRYPT_PID)..."
        kill "$DNSCRYPT_PID" 2>/dev/null || true
        wait "$DNSCRYPT_PID" 2>/dev/null || true
    fi

    # Re-enable systemd-resolved cache
    if [[ -f "$RESOLVED_CONF_DROP" ]]; then
        echo "Re-enabling systemd-resolved cache..."
        sudo rm -f "$RESOLVED_CONF_DROP"
        sudo systemctl restart systemd-resolved
    fi

    # Restore original DNS
    if [[ -n "$ORIGINAL_DNS" ]]; then
        echo "Restoring DNS for $IFACE to: $ORIGINAL_DNS"
        sudo resolvectl dns "$IFACE" $ORIGINAL_DNS
    else
        echo "Reverting $IFACE DNS to DHCP default..."
        sudo resolvectl revert "$IFACE"
    fi

    echo "Current DNS status:"
    resolvectl dns "$IFACE"
    echo "=== Done ==="
}

trap cleanup EXIT

# --- Prompt for sudo upfront ---
echo "This script needs sudo to change DNS settings."
sudo -v

# --- Save current DNS ---
ORIGINAL_DNS="$(resolvectl dns "$IFACE" 2>/dev/null | sed "s/^.*): //")"
echo "Saved original DNS for $IFACE: $ORIGINAL_DNS"

# --- Disable systemd-resolved cache ---
# Without this, resolved sits between Firefox and dnscrypt-proxy and caches
# responses, hiding the real ODoH latency from the Performance API.
echo "Disabling systemd-resolved cache..."
sudo mkdir -p /etc/systemd/resolved.conf.d
echo -e "[Resolve]\nCache=no" | sudo tee "$RESOLVED_CONF_DROP" > /dev/null
sudo systemctl restart systemd-resolved

# --- Start dnscrypt-proxy ---
echo "Starting dnscrypt-proxy (ODoH) on $DNSCRYPT_LISTEN..."
"$DNSCRYPT_BIN" -config "$DNSCRYPT_CONFIG" &
DNSCRYPT_PID=$!

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

# --- Override system DNS ---
echo "Setting DNS for $IFACE to $DNSCRYPT_LISTEN..."
sudo resolvectl dns "$IFACE" "$DNSCRYPT_LISTEN"
resolvectl dns "$IFACE"

# Verify — flush first so the dig actually goes through ODoH
sudo resolvectl flush-caches
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
python benchmark.py odoh

echo ""
echo "Benchmark complete. Results in results_odoh.csv"
