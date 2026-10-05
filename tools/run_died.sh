#!/usr/bin/env bash
# Tells the health chat that an hourly run could not do its job, from outside
# Python: the last step of .github/workflows/price-monitor.yml, run when
#
#   the monitor never ran - a step before it failed (checkout, setup, the open
#   months of the bars) - so nothing was delivered and nothing was counted; or
#   the state was not committed, so what the run sent and the health streak it
#   counted are lost, and the next run may send the same messages again.
#
# The monitor's own health report cannot say either: in the first case it did
# not run, in the second its record is the thing that was lost.
#
# Once on the first such run, then every REMIND_EVERY in a row: how many in a
# row is read off the earlier runs of this workflow, as the runs in which this
# step ran (the workflow runs it only on such a run). Any doubt there - an API error, a run still going - counts as
# a fresh outage, so the message goes out.
#
# Needs STEPS (the workflow's toJSON(steps)), TELEGRAM_BOT_TOKEN,
# TELEGRAM_HEALTH_CHAT_ID, GH_TOKEN with actions: read, RUN_URL, and
# GITHUB_REPOSITORY, GITHUB_REF_NAME, GITHUB_RUN_ID. Never prints the token.
set -uo pipefail

REPORT_STEP="Tell the health chat when the run could not deliver"
WORKFLOW_FILE="price-monitor.yml"
REMIND_EVERY=24

# The steps whose failure ends the run before delivery, by id, as named in the
# workflow.
name_of() {
  case "$1" in
    checkout) echo "Checkout" ;;
    python) echo "Set up Python" ;;
    deps) echo "Install dependencies" ;;
    sessions) echo "Extend the session table when it runs short" ;;
    cache) echo "Restore gitignored Tremor parquet" ;;
    cache_mark) echo "Mark when the cache landed" ;;
    hot) echo "Restore the open months of the bars" ;;
    tremor) echo "Tremor pipeline" ;;
    hot_save) echo "Save the open months of the bars" ;;
    monitor) echo "Run monitor" ;;
    commit) echo "Commit updated state" ;;
    *) echo "$1" ;;
  esac
}

outcome() { jq -r --arg id "$1" '.[$id].outcome // ""' <<<"$STEPS"; }

monitor=$(outcome monitor)
commit=$(outcome commit)
if [ "$monitor" != "success" ] && [ "$monitor" != "failure" ]; then
  # Not the continue-on-error steps: their failure does not stop the run.
  first=$(jq -r '[to_entries[] | select(.key != "sessions" and .key != "tremor")
                 | select(.value.outcome == "failure" or .value.outcome == "cancelled")
                 | .key][0] // ""' <<<"$STEPS")
  if [ -n "$first" ]; then
    why="it stopped at \"$(name_of "$first")\""
  else
    why="it was cancelled or ran out of time before the monitor"
  fi
  text="⚠️ Hourly run could not deliver: ${why}. Nothing was sent to the channel and no health was counted."
elif [ "$commit" = "failure" ]; then
  text="⚠️ Hourly run delivered but could not commit its state (\"$(name_of commit)\" failed). What it sent and the health it counted are lost; the next run may send the same messages again."
else
  echo "the monitor ran and its state was committed: nothing to report"
  exit 0
fi

# How many runs in a row this step has reported, this one included.
streak=1
runs=$(gh api "repos/$GITHUB_REPOSITORY/actions/workflows/$WORKFLOW_FILE/runs?branch=$GITHUB_REF_NAME&per_page=$((REMIND_EVERY + 1))" \
         --jq ".workflow_runs[] | select(.id != $GITHUB_RUN_ID) | .id" 2>/dev/null) || runs=""
for id in $runs; do
  reported=$(gh api "repos/$GITHUB_REPOSITORY/actions/runs/$id/jobs" \
               --jq "[.jobs[].steps[] | select(.name == \"$REPORT_STEP\") | .conclusion][0] // \"\"" \
               2>/dev/null) || break
  [ "$reported" = "success" ] || break
  streak=$((streak + 1))
done
if [ "$streak" -gt 1 ] && [ $(((streak - 1) % REMIND_EVERY)) -ne 0 ]; then
  echo "run $streak in a row that could not deliver; reminded every $REMIND_EVERY"
  exit 0
fi
[ "$streak" -gt 1 ] && text="$text ($streak runs in a row.)"
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
