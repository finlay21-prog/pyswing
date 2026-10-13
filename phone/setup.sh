#!/data/data/com.termux/files/usr/bin/bash
# One-off setup in Termux on Android. Usage: bash pyswing/phone/setup.sh <ntfy-topic>
set -e

TOPIC="$1"
if [ -z "$TOPIC" ]; then
  echo "Usage: bash pyswing/phone/setup.sh <your-ntfy-topic>"
  exit 1
fi

echo "== Installing Python, git and Termux:API tools"
pkg install -y python git termux-api
command -v pip >/dev/null || pkg install -y python-pip
pip install -q requests

[ -d "$HOME/pyswing" ] || git clone -q https://github.com/finlay21-prog/pyswing "$HOME/pyswing"
chmod +x "$HOME/pyswing/phone/run.sh"
echo "NTFY_TOPIC=$TOPIC" > "$HOME/.pyswing.env"

echo "== First check (records what's free now, no alerts)"
"$HOME/pyswing/phone/run.sh"
tail -n 1 "$HOME/pyswing-runs.log"
if grep -q ' error: ' "$HOME/pyswing-last-run.log"; then
  echo "!! ClubSpark couldn't be reached from this connection:"
  grep ' error: ' "$HOME/pyswing-last-run.log"
  exit 1
fi
grep -A2 '^## London Fields' "$HOME/pyswing-last-run.log" || true

echo "== Scheduling hourly checks"
if timeout 30 termux-job-scheduler --job-id 1 --period-ms 3600000 --network any \
     --persisted true --script "$HOME/pyswing/phone/run.sh"; then
  echo "Done. Check it's running any time with: tail ~/pyswing-runs.log"
else
  echo "!! Couldn't schedule the job. Is the Termux:API app installed (from the same place as Termux)?"
  exit 1
fi
