#!/bin/bash
# Install (or reinstall) the launchd job that runs desk-collect on weekdays at
# 8:00am and 5:00pm New York time.
#
#   scripts/install-schedule.sh                  # finds desk-collect on your PATH
#   scripts/install-schedule.sh /path/to/desk-collect
#   scripts/install-schedule.sh --uninstall
#
# Rerun it after moving the project folder or changing the Mac's time zone.

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

# The hour on this Mac when it's $1 o'clock in New York. Offsets are like -0700;
# both are read today, so daylight saving time is already counted.
local_hour_for_new_york() {
    local here ny
    here=$(date +%z)
    ny=$(TZ=America/New_York date +%z)
    to_minutes() { local s=${1:0:1} h=$((10#${1:1:2})) m=$((10#${1:3:2})); echo "${s}$((h * 60 + m))"; }
    local diff=$(( $(to_minutes "$here") - $(to_minutes "$ny") ))
    if (( diff % 60 != 0 )); then
        echo "This time zone isn't a whole number of hours from New York; edit the hours by hand." >&2
        exit 1
    fi
    echo $(( ($1 + diff / 60 + 24) % 24 ))
}
MORNING_HOUR=$(local_hour_for_new_york 8)
EVENING_HOUR=$(local_hour_for_new_york 17)

chmod +x "$PROJECT_DIR/scripts/run-digest.sh"
mkdir -p "$PROJECT_DIR/data/logs" "$(dirname "$TARGET")"
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__DESK_COLLECT__|$DESK_COLLECT|g" \
    -e "s|__MORNING_HOUR__|$MORNING_HOUR|g" \
    -e "s|__EVENING_HOUR__|$EVENING_HOUR|g" \
    "$TEMPLATE" > "$TARGET"
plutil -lint "$TARGET" > /dev/null

launchctl bootstrap "$DOMAIN" "$TARGET"

echo "Installed $LABEL: weekdays at $MORNING_HOUR:00 and $EVENING_HOUR:00 this Mac's time"
echo "(8:00am and 5:00pm New York)."
echo "Run it now:   launchctl kickstart $DOMAIN/$LABEL"
echo "Logs:         $PROJECT_DIR/data/logs/desk-collect.log"
