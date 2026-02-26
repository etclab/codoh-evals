#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IFACE="${1:-enp113s0f0np0}"
DNSCRYPT_LISTEN="${2:-127.0.0.1:5300}"

usage() {
    cat <<'EOF'
Usage: ./set-dns.sh [INTERFACE] [DNSCRYPT_ADDRESS]

  INTERFACE          Network interface to configure (default: enp113s0f0np0)
  DNSCRYPT_ADDRESS   dnscrypt-proxy listen address (default: 127.0.0.1:5300)

Examples:
  ./set-dns.sh                                  # use defaults
  ./set-dns.sh eth0                             # custom interface, default address
  ./set-dns.sh eth0 127.0.0.1:5400             # custom interface and address

What this script does (and why):

  1. Disables the systemd-resolved cache by calling disable-resolved-cache.sh.
     systemd-resolved sits between applications and the upstream DNS server.
     If its cache is enabled, repeated lookups return instantly from the cache
     instead of going through dnscrypt-proxy, hiding the real ODoH latency
     from timing measurements.

  2. Sets the interface's DNS server to the dnscrypt-proxy listen address
     using "resolvectl dns". This tells systemd-resolved to forward queries
     for this interface to dnscrypt-proxy instead of the default upstream.

  3. Sets the routing domain to "~." on the interface using "resolvectl domain".
     Without this, systemd-resolved may race queries against other DNS servers
     (e.g. 8.8.8.8 in the global config) and use whichever responds first,
     bypassing dnscrypt-proxy entirely. The "~." routing domain forces ALL
     domain lookups to go through this interface's DNS server.

  4. Flushes the systemd-resolved cache to ensure stale entries from before
     the switch don't pollute results.

To revert:
  sudo resolvectl revert <INTERFACE>
  sudo rm -f /etc/systemd/resolved.conf.d/no-cache.conf
  sudo systemctl restart systemd-resolved
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

echo "=== Configuring DNS for ODoH benchmarking ==="
echo "  Interface: $IFACE"
echo "  DNS:       $DNSCRYPT_LISTEN"
echo ""

# Step 1: Disable systemd-resolved cache
"$SCRIPT_DIR/disable-resolved-cache.sh"
echo ""

# Step 2: Point interface DNS to dnscrypt-proxy
echo "Current DNS for $IFACE:"
resolvectl dns "$IFACE"

echo "Setting DNS for $IFACE to $DNSCRYPT_LISTEN..."
sudo resolvectl dns "$IFACE" "$DNSCRYPT_LISTEN"

# Step 3: Route all domains through this interface
echo "Setting routing domain to ~. for $IFACE..."
sudo resolvectl domain "$IFACE" "~."

# Step 4: Flush any stale cached entries
echo "Flushing DNS cache..."
sudo resolvectl flush-caches

echo ""
echo "=== Done ==="
echo "Updated DNS for $IFACE:"
resolvectl dns "$IFACE"
resolvectl domain "$IFACE"
