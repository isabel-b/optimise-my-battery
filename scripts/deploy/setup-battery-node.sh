#!/bin/bash
# One-time: give the battery tracker its own Tailscale hostname.
# Run as: sudo ./scripts/deploy/setup-battery-node.sh
# Starts a second tailscaled (userspace networking, separate state and socket), joins it
# to the tailnet as "battery" (you will be given a login URL to open), and serves the
# dashboard at https://battery.<tailnet>.ts.net. Safe to re-run.
set -euo pipefail
TS=/opt/homebrew/opt/tailscale/bin/tailscale
SOCK=/var/run/tailscaled-battery.sock
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.isabel.tailscaled-battery.plist"
PLIST_DST=/Library/LaunchDaemons/com.isabel.tailscaled-battery.plist
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo $0" >&2; exit 1; }

mkdir -p /var/lib/tailscale-battery /opt/homebrew/var/log
cp "$PLIST_SRC" "$PLIST_DST"; chown root:wheel "$PLIST_DST"; chmod 644 "$PLIST_DST"
launchctl bootout system/com.isabel.tailscaled-battery 2>/dev/null || true
launchctl bootstrap system "$PLIST_DST"
for i in $(seq 1 20); do [ -S "$SOCK" ] && break; sleep 1; done
[ -S "$SOCK" ] || { echo "second tailscaled did not start; see /opt/homebrew/var/log/tailscaled-battery.log" >&2; exit 1; }

echo
echo "Joining the tailnet as 'battery'. If a login URL is printed, open it in a browser and approve."
"$TS" --socket="$SOCK" up --hostname=battery
echo
"$TS" --socket="$SOCK" serve --bg 5050
"$TS" --socket="$SOCK" serve status
echo
echo "Removing the old port-8443 mapping from the main node (ignore an error if it was not set):"
"$TS" serve --https=8443 off || true
echo
echo "Done. Open https://battery.$("$TS" --socket="$SOCK" status --json | sed -n 's/.*"MagicDNSSuffix": *"\([^"]*\)".*/\1/p' | head -1)/ on the phone."
