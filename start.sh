#!/usr/bin/env bash
# Token Firewall v3: auto-restart launcher
cd "$(dirname "$0")"
source ~/.token-firewall.env 2>/dev/null || true

echo "Starting Token Firewall v3 (auto-restart enabled)..."
while true; do
    python server.py
    echo "[$(date)] Crashed or stopped, restarting in 3s..."
    sleep 3
done
