#!/bin/bash
# Starts the dashboard. Invoked by launchd, which keeps it alive.
set -euo pipefail
REPO_DIR="${OMB_REPO:-$HOME/optimise-my-battery}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export HOST="${HOST:-0.0.0.0}"     # reachable over the LAN and Tailscale; laptop default stays 127.0.0.1
export PORT="${PORT:-5050}"
export PYTHONUNBUFFERED=1           # so the launchd log shows output as it happens
cd "$REPO_DIR"
exec "$REPO_DIR/.venv/bin/python" web.py
