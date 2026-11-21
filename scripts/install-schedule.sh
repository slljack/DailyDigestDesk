#!/bin/bash
# Install (or reinstall) the launchd job that runs desk-collect on weekdays at
# 8:00am and 5:00pm New York time. launchd checks every 15 minutes and
# desk-collect --if-due decides whether a run is due, so a run the Mac slept
# through happens once it's awake.
#
#   scripts/install-schedule.sh                  # finds desk-collect on your PATH
#   scripts/install-schedule.sh /path/to/desk-collect
#   scripts/install-schedule.sh --uninstall
#
# Rerun it after moving the project folder.

set -euo pipefail

LABEL="com.dailydigestdesk.collect"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$PROJECT_DIR/scripts/$LABEL.plist.template"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

# Remove the old job first, so a reinstall picks up the new file. Fine if none is loaded.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$TARGET"
    echo "Uninstalled $LABEL."
    exit 0
fi

DESK_COLLECT="${1:-$(command -v desk-collect || true)}"
if [ -z "$DESK_COLLECT" ] || [ ! -x "$DESK_COLLECT" ]; then
    echo "Can't find desk-collect. Activate the conda env first, or pass its full path:" >&2
    echo "  scripts/install-schedule.sh ~/miniconda3/envs/daily-digest-desk/bin/desk-collect" >&2
    exit 1
fi

chmod +x "$PROJECT_DIR/scripts/run-digest.sh"
mkdir -p "$PROJECT_DIR/data/logs" "$(dirname "$TARGET")"
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__DESK_COLLECT__|$DESK_COLLECT|g" \
    "$TEMPLATE" > "$TARGET"
plutil -lint "$TARGET" > /dev/null

launchctl bootstrap "$DOMAIN" "$TARGET"

echo "Installed $LABEL: checks every 15 minutes, runs at 8:00am and 5:00pm New York"
echo "on weekdays (or at the first check after, if the Mac was asleep)."
echo "Run it now, due or not:   $PROJECT_DIR/scripts/run-digest.sh"
echo "Logs:                     $PROJECT_DIR/data/logs/desk-collect.log"
