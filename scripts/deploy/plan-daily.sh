#!/bin/bash
# Runs the force-charge planner just before the cheap window opens (launchd, 10:45).
# Dry run unless the git-ignored file .apply-schedule exists in the repo root: that file
# is the switch that lets the planner write to the inverter.
set -euo pipefail
REPO_DIR="${OMB_REPO:-$HOME/optimise-my-battery}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$REPO_DIR"
APPLY=""; [ -f "$REPO_DIR/.apply-schedule" ] && APPLY="--apply"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) planner start ${APPLY:-(dry run)}"
"$REPO_DIR/.venv/bin/python" planner.py $APPLY
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) planner done"
