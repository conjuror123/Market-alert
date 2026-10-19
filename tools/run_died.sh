#!/usr/bin/env bash
# Tells the health chat that an hourly run's state was not committed, from
# outside Python: the last step of .github/workflows/price-monitor.yml. Either
#
#   the monitor never ran - a step before it failed (checkout, setup, the open
#   months of the bars) - so nothing was delivered and nothing was counted; or
#   it ran but the commit did not succeed, so what it sent and the health it
#   counted are lost, and the next run may send the same messages again.
#
# The monitor cannot say either: in the first case it did not run, in the
# second its record is the thing that was lost.
#
# TIMED BY THE ONE CLOCK the missed-runs check reads: last_run_utc in the
# state this run started from - the last run whose state was committed. Sent
# when that is 1, 25, 49... hours ago: on the first such run, then daily.
# With no record to read, sent.
#
# Needs STEPS (the workflow's toJSON(steps)), TELEGRAM_BOT_TOKEN,
# TELEGRAM_HEALTH_CHAT_ID, RUN_URL and GITHUB_SHA. Never prints the token.
set -uo pipefail

# The steps that can stop the run before delivery, by id, as named in the
# workflow.
name_of() {
  case "$1" in
    checkout) echo "Checkout" ;;
    python) echo "Set up Python" ;;
    deps) echo "Install dependencies" ;;
    sessions) echo "Extend the session table when it runs short" ;;
    cache) echo "Restore gitignored Jump parquet" ;;
    cache_mark) echo "Mark when the cache landed" ;;
    hot) echo "Restore the open months of the bars" ;;
    jump) echo "Jump pipeline" ;;
    hot_save) echo "Save the open months of the bars" ;;
    monitor) echo "Run monitor" ;;
    commit) echo "Commit updated state" ;;
    *) echo "$1" ;;
  esac
}

outcome() { jq -r --arg id "$1" '.[$id].outcome // ""' <<<"$STEPS"; }

monitor=$(outcome monitor)
if [ "$(outcome commit)" = "success" ]; then
  echo "the state was committed: nothing to report"
  exit 0
elif [ "$monitor" != "success" ] && [ "$monitor" != "failure" ]; then
  # Not the continue-on-error steps: their failure does not stop the run.
  first=$(jq -r '[to_entries[] | select(.key != "sessions" and .key != "jump")
                 | select(.value.outcome == "failure" or .value.outcome == "cancelled")
                 | .key][0] // ""' <<<"$STEPS")
  if [ -n "$first" ]; then
    why="it stopped at \"$(name_of "$first")\""
  else
    why="it was cancelled or ran out of time before the monitor"
  fi
  text="⚠️ Hourly run could not deliver: ${why}. Nothing was sent to the channel and no health was counted."
else
  text="⚠️ Hourly run delivered but could not commit its state (\"$(name_of commit)\" did not succeed). What it sent and the health it counted are lost; the next run may send the same messages again."
fi

# The state this run started from: after a failed commit the file on disk is
# already this run's own.
last=$(git show "${GITHUB_SHA}:data/state.json" 2>/dev/null \
         | jq -r '._monitoring_health.last_run_utc // empty' 2>/dev/null)
if [ -n "$last" ]; then
  hours=$((($(date +%s) - last + 1800) / 3600))
  if [ $((hours % 24)) -ne 1 ]; then
    echo "$hours h since the last run that delivered; said at 1 h, then every 24"
    exit 0
  fi
  text="$text ($hours h since the last run that delivered.)"
fi
text="$text
$RUN_URL"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_HEALTH_CHAT_ID:-}" ]; then
  echo "::warning::no health chat configured; not reported: $text"
  exit 0
fi
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
         "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
         --data-urlencode "chat_id=${TELEGRAM_HEALTH_CHAT_ID}" \
         --data-urlencode "text=${text}" \
         --data-urlencode "disable_web_page_preview=true")
if [ "$code" != "200" ]; then
  echo "::error::Telegram answered $code; not reported: $text"
  exit 1
fi
echo "reported: $text"
