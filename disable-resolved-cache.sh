#!/usr/bin/env bash

set -euo pipefail

RESOLVED_CONF_DROP="/etc/systemd/resolved.conf.d/no-cache.conf"

echo "Disabling systemd-resolved cache..."
sudo mkdir -p /etc/systemd/resolved.conf.d
echo -e "[Resolve]\nCache=no" | sudo tee "$RESOLVED_CONF_DROP" > /dev/null
sudo systemctl restart systemd-resolved

echo "Cache disabled. Verify with:"
echo "  resolvectl statistics"
