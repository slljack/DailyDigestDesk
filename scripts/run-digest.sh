#!/bin/bash
# Run desk-collect once, append its output to a log, and show a pop-up to open the page.
#
# launchd runs this on a schedule (see install-schedule.sh). You can also run it
# by hand from anywhere: scripts/run-digest.sh
#
# Exits with desk-collect's exit code, so launchd's log shows failed runs too.

set -u

# A scheduled job gets a bare environment: no conda, no Homebrew on the PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# The project folder, so holdings.toml, .env and data/ are found.
cd "$(dirname "$0")/.." || exit 1

# The installer sets DESK_COLLECT to the full path inside the conda env.
DESK_COLLECT="${DESK_COLLECT:-desk-collect}"
LOG="data/logs/desk-collect.log"
mkdir -p data/logs

output=$("$DESK_COLLECT" 2>&1)
status=$?

# API keys can show up in error messages (a failed request's url), and the log
# is a plain file: redact them before writing.
redacted=$(sed -E 's/(token|api_key)=[^&" ]+/\1=REDACTED/g; s/sk-ant-[A-Za-z0-9_-]+/REDACTED/g' <<< "$output")
{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z')  exit $status"
    echo "$redacted"
    echo
} >> "$LOG"

# desk-collect prints "# Digest page: /full/path.html" when it wrote one.
page=$(sed -n 's/^# Digest page: //p' <<< "$output")

if [ -z "$page" ]; then
    message="Failed: no page written. See $LOG"
else
    # The page's own summary line: "13 new items" or "Nothing new since the last digest."
    summary=$(sed -n 's/.*<p class="meta">[^·]*· \(.*\)<\/p>.*/\1/p' "$page")
    if [ "$status" -eq 0 ]; then
        message="$summary"
    else
        message="$summary (some sources failed, see $LOG)"
    fi
fi

# A pop-up, not a notification: current macOS silently drops notifications from
# scripts (no app behind them), but still shows alerts. "Open" opens the new page,
# or the log if no page was written. It closes by itself before the next run.
target="${page:-$PWD/$LOG}"
message=${message//\"/\'}  # a " would end the AppleScript string early
choice=$(osascript -e "display alert \"Daily digest\" message \"$message\" \
    buttons {\"Later\", \"Open\"} default button \"Open\" giving up after 28800" 2>&1)
if [[ "$choice" == *"button returned:Open"* ]]; then
    open "$target"
fi

exit "$status"
