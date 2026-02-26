#!/usr/bin/env bash

set -euo pipefail

IFACE="${1:-enp113s0f0np0}"
RESOLVED_CONF_DROP="/etc/systemd/resolved.conf.d/no-cache.conf"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Usage: ./unset-dns.sh [INTERFACE]

  INTERFACE   Network interface to revert (default: enp113s0f0np0)

Reverts the changes made by set-dns.sh:
  1. Removes the systemd-resolved no-cache drop-in config
  2. Reverts the interface DNS and routing domain to DHCP defaults
  3. Restarts systemd-resolved to apply changes
EOF
    exit 0
fi

echo "=== Reverting DNS configuration for $IFACE ==="

# Step 1: Re-enable systemd-resolved cache
if [[ -f "$RESOLVED_CONF_DROP" ]]; then
    echo "Removing no-cache config ($RESOLVED_CONF_DROP)..."
    sudo rm -f "$RESOLVED_CONF_DROP"
else
    echo "No cache config to remove."
fi

# Step 2: Revert interface DNS and routing domain to DHCP defaults
echo "Reverting DNS settings for $IFACE..."
sudo resolvectl revert "$IFACE"

# Step 3: Restart systemd-resolved
echo "Restarting systemd-resolved..."
sudo systemctl restart systemd-resolved

echo ""
echo "=== Done ==="
echo "DNS for $IFACE:"
resolvectl dns "$IFACE"
resolvectl domain "$IFACE"
