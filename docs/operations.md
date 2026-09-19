# Running it

What the live system does, what it costs, and what to look at when something is wrong.

---

## The trigger

An external service (cron-job.org) calls `workflow_dispatch` on `price-monitor.yml`
through the GitHub API, once an hour at five past. It posts `{"ref": "<default branch>"}`
to `/repos/conjuror123/Market-alert/actions/workflows/price-monitor.yml/dispatches` with an
`Authorization: Bearer <PAT>` header. If the branch name changes, that body is the one
thing to update.

**GitHub's own `schedule:` is deliberately not used.** On this repository it fires roughly
once every ten hours rather than once an hour, and a trigger that unpredictably does not
fire is worse than none — its silence is indistinguishable from a quiet market.

---

## What one run costs

| | |
|---|---:|
| Live fetch (Tiingo + Yahoo + Coinbase) | seconds |
| Metrics and events | ~2 min |
| Whole job, median | ~2 min |
| Our own timeout | 20 min |
| GitHub's hard cap | 6 hours |

No live fetch waits on a rate limit. Tiingo answers in about 0.28 s with no enforced
throttle; Yahoo and Coinbase have no pacing requirement. A `config_version` mismatch
forces a cold rebuild of the derived data, which takes the same two minutes — that is the
guard working, not a fault.

---

## Quotas

| | Limit | Used |
|---|---:|---:|
| **Tiingo** | 50/hour, 1000/day | 37 instruments (29 US-session funds + 8 FX) |
| **Yahoo** | none published | 15 thin ETFs |
| **Coinbase** | no key | 9 crypto |
| **Twelve Data** | 800/day, 8/min | archive, gap-fill, deepening — not the hourly path |
| GitHub Actions minutes | unlimited (public repo) | — |
| Repository size | 1 GB warning, ~5 GB cutoff | ~730 MB |

Tiingo's hourly bucket is the binding live limit. The 44 US-session funds skip when the
NYSE calendar says no bar can have appeared since the newest stored one; the 8 FX pairs
skip when the Sun 17:00 → Fri 17:00 New York week is shut
(`tremor.backfill.nothing_can_have_appeared`). Crypto is never skipped.

---

## What gets committed, and when

**Every run:** `data/state.json` (which events have been sent — losing it re-sends them)
and `data/economic_calendar/`. The push rebases and retries if the branch moved, because a
rejected push is the same as losing the sent map. A truncated `state.json` fails the run
rather than being read as a cold start.

**At 04:00 UTC only:** `data/tremor/bars/` and `data/tremor/vix/`. Parquet rewrites files
whole, so hourly commits would add gigabytes a year for no new facts. Nothing is lost by
waiting: the forward fetch starts at each instrument's newest bar minus three hours, so a
day's worth is always recoverable. VIX is in that commit so the skip-if-fresh check can
see the latest close and avoid re-fetching CBOE's full 1990 file.

**Never:** `data/tremor/metrics/`, `residuals/`, `saed_events.parquet`. Derived,
gitignored, rebuilt in the run that needs them.

---

## When it breaks

The Tremor steps are `continue-on-error` so a fault in the detector cannot take down
delivery — and the job is **failed at the end anyway**. A pipeline that quietly stops
updating while the repository looks healthy is the failure this arrangement exists to
prevent.

If a fetch fails, `pipeline` and `saed` still run on the bars already stored, so healthy
instruments still get events. Delivery's 48-hour staleness rule drops what did not
refresh. Health does not record a clean run or send "recovered" while the Tremor step is
red — empty events would otherwise look like a quiet hour.

A provider failure names the instruments and their providers in a message to
`TELEGRAM_HEALTH_CHAT_ID`. The provider is not switched automatically. A Yahoo or Tiingo
rate limit that survives retries skips that provider's remaining instruments and is named
in the same message; a 404 stays a per-instrument dark.

**Two independent emails cover failure**, neither needing code:

- **cron-job.org** emails when it cannot reach GitHub — the HTTP call failed.
- **GitHub** emails on a failed workflow run — the call succeeded, the job did not.

**Open gap:** the health check counts consecutive *failures*, and only a run that executes
can increment one. If the trigger stops, nothing in the repository will say so; the
cron-job.org email is the only thing that would. A watchdog is designed and not built.

---

## Silence is the normal state

About 56 pushes a year across 61 instruments, arriving on ~35 days, median six days
apart, longest observed quiet stretch 85 days. A digest note opens Monday and Saturday at
00:05 UTC and fills as moves are found.

The economic-calendar forecast goes out once a week, in the same run immediately before
the Saturday note — so the note, the message that keeps changing, is the last one in the
chat. It takes its day from `tremor.routing` rather than a weekday of its own, so the pair
cannot be separated. It covers Monday 00:00 UTC to the following Monday, read from the
archive rather than the live feed, and consecutive digests abut exactly.

The calendar archive is topped up from the live feed once a day, separately from the
weekly digest: pushes name the releases in the three hours around a move on any day of the
week, and a schedule fetched last Saturday does not have the speech added on Wednesday.

**Nothing older than 48 hours is sent, and a note opens only in its own hour or the three
after.** This is load-bearing rather than tidy: the events table holds the whole history,
so without it the first run after a mute would deliver years of alerts at once. It also
does the right thing on a cold start. An empty events table sends nothing at all — it
cannot tell "nothing happened" from "the pipeline did not run".

So a long silence is the expected reading, not evidence of a fault. What distinguishes the
two is the Actions tab: green runs mean it looked and found nothing.

---

## Checking on it

```bash
python tools/report_card.py     # recall on the obvious, false alarms, frequency
```

Nothing imports that file and no threshold is set from its numbers. The alert count is a
thing to report afterwards, not a thing to steer by.

To run the whole pass by hand:

```bash
export TIINGO_API_KEY=...       # hourly bars
export TWELVEDATA_API_KEY=...   # archive / gap-fill / deepening
export FRED_API_KEY=...         # the VIX series only

python -m price_monitor.floor     # apply /floor before anything is scored
python -m tremor.backfill
python -m tremor.pipeline
python -m tremor.saed
python -m price_monitor.floor --reply
python -m price_monitor
```

---

## The mute

`tremor_alerts_muted` in `config/config.yaml`. Set it and the hourly run proceeds as
usual — bars are fetched, events are routed and written, health still reports — but
Tremor's pushes and digest do not go out.

It lives in the repository rather than on the scheduler's side deliberately: *"we are
deliberately silent"* is a state of the project and has to be visible where the code is. A
cron job switched off on someone else's website is indistinguishable from a breakage a
month later.
