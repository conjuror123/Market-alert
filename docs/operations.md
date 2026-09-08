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

| | |
|---|---:|
| Whole job | **~5m20s** |
| of which, the Tremor pipeline | ~5m |
| of which, waiting on the API rate limit | ~2m50s |
| Our own timeout | 20 min |
| GitHub's hard cap | 6 hours |

Roughly half the run is the job sleeping eight seconds between Twelve Data calls. Actual
compute is about two minutes.

---

## Quotas

| | Limit | Used |
|---|---:|---:|
| **Twelve Data** requests/day | 800 | **293 (37%)** |
| **Twelve Data** requests/minute | 8 | 7.5 |
| GitHub Actions minutes | unlimited (public repo) | — |
| Repository size | 1 GB warning, ~5 GB cutoff | **418 MB**, +0.36 GB/year |

The daily budget is 12 US equities at 77/day plus 9 FX pairs at 216/day. The equities
figure is low because the forward fetch **skips an instrument when the NYSE calendar says
no bar can have appeared** since its newest stored one — without that it would be 288 and
the total 504.

FX is never skipped: it has no session table here, and its Sunday reopen is exactly the
edge a hand-written rule would get wrong. Crypto is free — Coinbase needs no key.

The per-minute ceiling is the binding one. Twenty-one instruments at eight seconds apart
is what makes the run five minutes rather than two.

---

## What gets committed, and when

**Every run:** `data/state.json` (which events have been sent — losing it re-sends them)
and `data/economic_calendar/`.

**At 04:00 UTC only:** the bar archive and the event tables. Parquet rewrites files
whole, so hourly commits would add gigabytes a year for no new facts. Nothing is lost by
waiting: the forward fetch measures its window from each instrument's own newest bar, so
a day's worth is always recoverable.

**Never:** `data/tremor/metrics/`, `residuals/`, `events/`. Derived, gitignored, rebuilt
in the run that needs them.

---

## When it breaks

**The Tremor steps are `continue-on-error`** so a fault in the detector cannot take down
delivery — but the job is **failed at the end anyway**. A pipeline that quietly stops
updating while the repository looks healthy is the exact failure this arrangement exists
to prevent.

If the pipeline fails, the events table is left as it was, and delivery's 48-hour
staleness rule then sends nothing rather than something wrong.

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
124 days. A digest note opens Tuesday and Friday at 12:00 Israel time and fills in as
moves are found, 3.6 items each by the time its period closes. The
economic-calendar forecast goes out once a week, on Friday at 12:00 immediately before
the note opens, so the note - the message that keeps changing - is the last one in the
chat.

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
export TWELVEDATA_API_KEY=...   # equities and FX
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
