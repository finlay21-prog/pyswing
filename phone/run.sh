#!/data/data/com.termux/files/usr/bin/bash
# Hourly ClubSpark check, run on the phone by Termux's job scheduler (see phone/setup.sh).
# ClubSpark blocks cloud servers, so these venues are checked from the phone's own connection.

cd "$HOME/pyswing" || exit 1
git pull -q --ff-only 2>/dev/null  # pick up code changes pushed to GitHub

set -a
. "$HOME/.pyswing.env"  # NTFY_TOPIC=...
set +a

PLATFORMS=clubspark \
STATE_FILE="$HOME/.pyswing-state.json" \
TZ=Europe/London \
python court_watch.py > "$HOME/pyswing-last-run.log" 2>&1
status=$?

# One line per run, so you can check it's running: tail ~/pyswing-runs.log
echo "$(date '+%a %d %b %H:%M') exit=$status errors=$(grep -c ' error: ' "$HOME/pyswing-last-run.log")" >> "$HOME/pyswing-runs.log"
tail -n 500 "$HOME/pyswing-runs.log" > "$HOME/.pyswing-runs.tmp" && mv "$HOME/.pyswing-runs.tmp" "$HOME/pyswing-runs.log"
