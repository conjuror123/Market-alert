# Running it

What the live system does, what it costs, and what to look at when something is wrong.

---

## The trigger

An external service (cron-job.org) calls `workflow_dispatch` on `price-monitor.yml`
through the GitHub API, once an hour at five past.

**GitHub's own `schedule:` is deliberately not used.** Measured on this repository it
fired roughly once every ten hours rather than once an hour, and a trigger that
unpredictably does not fire is worse than none — its silence is indistinguishable from a
quiet market.

The cron job posts `{"ref": "<default branch>"}` to
`/repos/conjuror123/Market-alert/actions/workflows/price-monitor.yml/dispatches`, with an
`Authorization: Bearer <PAT>` header. If the branch name ever changes, that body is the
one thing to update.

---

## What one run costs

Live bars no longer wait on Twelve Data's 8-second pause. The hourly fetch is Tiingo
(29 US-session funds + 8 FX), Yahoo (15 thin ETFs) and Coinbase (9 crypto). Tiingo
measured 0.28 s/request with no enforced throttle; its **50 requests/hour** bucket is
the binding live limit, and a US-session hour asks 37 of those 50. Yahoo has no
published quota. Coinbase needs no key.

The Tremor pipeline (metrics → events) rebuilds derived parquet in about two minutes.
A `config_version` mismatch forces a cold rebuild of the same length rather than an
extend that would keep stale columns.

| | |
|---|---:|
| Live fetch (Tiingo + Yahoo + Coinbase) | seconds, not minutes of sleeping |
| Tremor pipeline | ~2 min |
| Our own timeout | 20 min |
| GitHub's hard cap | 6 hours |

---

## Quotas

| | Limit | Used |
|---|---:|---:|
| **Tiingo** | 50/hour, 1000/day | 37 live instruments (29 US-session funds + 8 FX) |
| **Yahoo** | none published | 15 thin ETFs |
| **Coinbase** | no key | 9 crypto |
| **Twelve Data** | 800/day, 8/min | archive, gap-fill, deepening — not the hourly path |
| GitHub Actions minutes | unlimited (public repo) | — |
| Repository size | 1 GB warning, ~5 GB cutoff | **~730 MB** (GitHub, 2026-09-15) |

44 US-session funds skip when the NYSE calendar says no bar can have appeared since
the newest stored one. The 8 FX pairs skip when the Sun 17:00 → Fri 17:00 New York
week is shut (`tremor.backfill.nothing_can_have_appeared`). Crypto is never skipped.

Twelve Data remains the source for `--extend-history`, `--fill-gaps` and the
deepening modes that the live providers do not cover.

---

## What gets committed, and when

**Every run:** `data/state.json` (which events have been sent — losing it re-sends them)
and `data/economic_calendar/`. The hourly push rebases and retries if the branch
moved, because a rejected push is the same as losing the sent map. A truncated
`state.json` fails the run rather than being read as a cold start.

**At 04:00 UTC only:** `data/tremor/bars/` (year-sharded hourly parquet) and
`data/tremor/vix/`. Event tables are gitignored and rebuilt in the run that needs
them. Parquet rewrites files whole, so hourly commits would add gigabytes a year for
no new facts. Nothing is lost by waiting: the forward fetch starts at each
instrument's newest bar minus three hours, so a day's worth is always recoverable.
VIX is in that daily commit so skip-if-fresh can see the latest close and skip
CBOE's full 1990 file.

**Never:** `data/tremor/metrics/`, `residuals/`, `events/`. Derived, gitignored, rebuilt
in the run that needs them.

---

## When it breaks

**The Tremor steps are `continue-on-error`** so a fault in the detector cannot take down
delivery — but the job is **failed at the end anyway**. A pipeline that quietly stops
updating while the repository looks healthy is the exact failure this arrangement exists
to prevent.

If a fetch fails, pipeline and saed still run on the bars already stored, so
healthy instruments still get events. The job is failed at the end. Delivery's
48-hour staleness rule still drops events that did not refresh. Health does
not send "recovered" or record a clean run when the Tremor step is red: empty
events would otherwise look like a quiet hour.

A provider failure now also names the instruments (and their providers) in a
Telegram message to `TELEGRAM_HEALTH_CHAT_ID`, falling back to `TELEGRAM_CHAT_ID`
until that secret exists. The product channel is not used for diagnostics once
the health chat is set. The job still fails; the provider is not switched
automatically. A Yahoo 429 after retries skips the remaining Yahoo instruments
the same way a spent Tiingo bucket does, and is named in that ops message; a
404 stays a per-instrument dark.

**Two independent emails cover failure**, and neither needs code:

- **cron-job.org** emails when it cannot reach GitHub — the HTTP call failed.
- **GitHub** emails on a failed workflow run — the call succeeded but the job did not.

**The gap that is still open:** the health check counts consecutive *failures*. It cannot
report runs that never *happened*. The trigger stopped on 2026-09-01 and it took six days
to notice. Nothing in the repository will tell you; the cron-job.org email is the only
thing that would.

---

## Silence is the normal state

About **22 pushes a year**, a median of ten days apart, longest observed quiet stretch
124 days. A digest note opens Monday and Saturday at 00:05 UTC and fills in as
moves are found, 3.6 items each by the time its period closes. The
economic-calendar forecast goes out once a week, at the Saturday opening and in the same
run immediately before it, so the note - the message that keeps changing - is the last
one in the chat. It takes its day from tremor.routing rather than from a weekday of its
own, so the pair cannot be separated by editing one of them. It covers the next whole
week, Monday 00:00 UTC to the following Monday 00:00 UTC - so a digest sent on Saturday
the 1st lists the 3rd through the 9th - read out of the archive rather than out of the
live feed. Consecutive digests abut exactly: the weekend a digest is sent in looks
dropped and is not, because the previous week's digest listed it seven days earlier.

**The economic-calendar archive is topped up from the live feed once a day**, separately
from the weekly digest. The digest refreshes it when it sends, which is often enough for a
message about next week and far too seldom for the pushes: they name the releases in the
three hours around a move on every day of the week, and a schedule fetched last Saturday
does not have the speech added on Wednesday. Three ForexFactory monthly pages are read on
the weekly send - the two behind for released values, the one ahead so the coming week is
there to list.

**No push is sent that is more than 48 hours old, and a note is only opened in its own
hour or the three after it** — a period that misses that window is carried into the next
note rather than arriving at some arbitrary time of day. This is load-bearing rather than
tidy: the events table holds
the whole history, so without it the first run after a mute would deliver years of alerts
at once. It also does the right thing on a cold start, where there is no record of what
was sent. An empty events table sends nothing at all — it cannot tell "nothing happened"
from "the pipeline did not run".

So a long silence is the expected reading, not evidence of a fault. What distinguishes
the two is the Actions tab: green runs mean it looked and found nothing.

---

## Checking on it

```bash
python tools/report_card.py     # recall on the obvious, false alarms, frequency
```

Nothing imports that file and no threshold is set from its numbers. The alert count is a
thing to report afterwards, not a thing to steer by.

To run the whole pass by hand:

```bash
export TWELVEDATA_API_KEY=...   # archive / gap-fill / deepening, not the hourly path
export TIINGO_API_KEY=...       # 29 US-session funds and 8 FX, hourly
export FRED_API_KEY=...         # the VIX series only

python -m tremor.backfill
python -m tremor.pipeline
python -m tremor.cross_section
python -m tremor.saed
python -m tremor.cluster
python -m price_monitor
```

Skipping `tremor.cluster` does not merely omit a step: it leaves
`metrics_basket_hour.parquet` stripped of the eighteen columns `cluster` writes into it.

---

## The mute

`tremor_alerts_muted` in `config/config.yaml`. It lives in the repository rather than on
the scheduler's side deliberately: *"we are deliberately silent"* is a state of the
project and has to be visible where the code is. A cron job switched off on someone
else's website looks like a breakage a month later, and there is nobody left to tell
which it was.
