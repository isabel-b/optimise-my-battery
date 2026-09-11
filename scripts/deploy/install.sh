#!/bin/bash
# One-time installer on the Mac mini. Run as: sudo ./scripts/deploy/install.sh
# Installs three LaunchDaemons (server, nightly fetch, 2-minute deploy) so they
# run at boot without anyone logged in. Re-running is safe: it reloads them.
set -euo pipefail
RUN_USER="${SUDO_USER:-$(whoami)}"
REPO_DIR="${OMB_REPO:-$(eval echo "~$RUN_USER")/optimise-my-battery}"
DAEMONS="/Library/LaunchDaemons"
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo so the jobs survive a reboot: sudo $0" >&2; exit 1; fi
mkdir -p "$REPO_DIR/scripts/deploy/logs"; chown -R "$RUN_USER" "$REPO_DIR/scripts/deploy/logs"
chmod +x "$REPO_DIR"/scripts/deploy/*.sh
echo "Installing for user '$RUN_USER' from $REPO_DIR"
for label in server fetch plan deploy; do
    SRC="$REPO_DIR/scripts/deploy/com.isabel.optimise-my-battery.${label}.plist"
    DST="$DAEMONS/com.isabel.optimise-my-battery.${label}.plist"
    sed -e "s|REPLACE_WITH_REPO_PATH|$REPO_DIR|g" -e "s|REPLACE_WITH_USER|$RUN_USER|g" "$SRC" > "$DST"
    chown root:wheel "$DST"; chmod 644 "$DST"
    launchctl bootout "system/com.isabel.optimise-my-battery.${label}" 2>/dev/null || true
    launchctl bootstrap system "$DST"
    echo "  loaded $label"
done
echo
echo "Dashboard: http://$(hostname -s).local:5050  (or the Tailscale IP)"
echo "Logs:      $REPO_DIR/scripts/deploy/logs/"
echo "Restart:   sudo launchctl kickstart -k system/com.isabel.optimise-my-battery.server"
