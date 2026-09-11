#!/bin/bash
# Nightly data pull, run by launchd at 00:30: yesterday's full day plus today so far,
# and the last two months of daily totals. About five API calls.
set -euo pipefail
REPO_DIR="${OMB_REPO:-$HOME/optimise-my-battery}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$REPO_DIR"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) fetch start"
"$REPO_DIR/.venv/bin/python" fetch.py history --days 3
"$REPO_DIR/.venv/bin/python" fetch.py report --months 2
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) fetch done"
