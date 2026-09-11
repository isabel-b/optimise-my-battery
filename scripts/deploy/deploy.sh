#!/bin/bash
# Periodic deploy on the Mac mini, run by launchd every 2 minutes as root:
# pulls main, reinstalls deps if requirements changed, restarts the server.
# Returns early when nothing changed.
set -euo pipefail
RUN_USER="${OMB_USER:-xavier}"
REPO_DIR="${OMB_REPO:-$(eval echo "~$RUN_USER")/optimise-my-battery}"
LABEL="com.isabel.optimise-my-battery.server"
as_user() { sudo -u "$RUN_USER" -H env PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }

cd "$REPO_DIR"
as_user git fetch origin main --quiet
LOCAL=$(as_user git rev-parse HEAD)
REMOTE=$(as_user git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) deploy: $LOCAL -> $REMOTE"
as_user git pull --ff-only --quiet origin main
if ! as_user git diff --quiet "$LOCAL" "$REMOTE" -- requirements.txt; then
  echo "requirements changed -> pip install"
  as_user "$REPO_DIR/.venv/bin/pip" install -q -r requirements.txt
fi
launchctl kickstart -k "system/$LABEL"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) deploy complete"
