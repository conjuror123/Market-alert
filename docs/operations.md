# Running it

> **Holds** how to run it: the trigger, quotas, what is committed when, and what to do
> when it breaks.
> **Does not hold** how it works (`architecture.md`) or why (`decisions.md`).
> **Keep it current.** Every number here is a live operational fact; a stale one sends
> somebody the wrong way at the worst moment.

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
| Metrics and events, warm run | ~5 s |
| Metrics and events, cold rebuild | ~15 s |
| Whole job, median | ~2 min |
| Our own timeout | 20 min |
| GitHub's hard cap | 6 hours |

No live fetch waits on a rate limit. Tiingo answers in about 0.28 s with no enforced
throttle; Yahoo and Coinbase have no pacing requirement. A `config_version` mismatch
forces a cold rebuild of the metrics (about ten seconds locally) — that is the guard
working, not a fault. The jump detector rescores the whole history every run, in about three
seconds, so it has no warm state to lose.

---

## Quotas

| | Limit | Used |
|---|---:|---:|
| **Tiingo** | 50/hour, 1000/day | 37 instruments (29 US-session funds + 8 FX) |
| **Yahoo** | none published | 15 thin ETFs |
| **Coinbase** | no key | 9 crypto |
| **Twelve Data** | 800/day, 8/min | archive, gap-fill, deepening — not the hourly path |
| GitHub Actions minutes | unlimited (public repo) | — |
| Repository size | 1 GB warning, ~5 GB cutoff | 565 MiB packed |

Tiingo's hourly bucket is the binding live limit. The 29 US-session funds skip when the
NYSE calendar says no bar can have appeared since the newest stored one; the 8 FX pairs
skip when the Sun 17:00 → Fri 17:00 New York week is shut
(`tremor.backfill.nothing_can_have_appeared`). Crypto is never skipped.

---

## What gets committed, and when

**Every run:** `data/state.json` (the week's messages on the channel — losing it posts a second note
and re-sends the moves of the last 24 hours),
`data/economic_calendar/`, and the dividend record: `data/tremor/corporate_actions.csv`
and `data/tremor/dividend_checks.csv`. On the first run after 09:30 New York,
`tremor.backfill` asks Yahoo for each paying fund's payouts; new ones go to
`corporate_actions.csv`, and `dividend_checks.csv` records how far each fund is confirmed.
A fund's overnight gap is not scored on a day its dividend is unconfirmed. When a fund is
five or more days behind, one line a day goes to the health chat. The push rebases and retries if the branch moved, because a
rejected push is the same as losing that record. A truncated `state.json` fails the run
rather than being read as a cold start.

**Saturday 04:00 UTC only:** `data/tremor/bars/` and `data/tremor/vix/`. Git cannot delta
parquet, so a commit stores every byte of whatever shard changed and the frequency is the
whole cost. Nothing is lost by waiting: the forward fetch starts at each instrument's
newest **stored** bar, so a week-old checkout is simply a week-wide request, and a week is
far inside every provider's reach — Tiingo serves 365 days, Yahoo 55 at thirty minutes.
Saturday because every market that keeps a session is shut, so the week written down is a
whole one. A missed Saturday is not a loss either — the next one re-fetches and commits the
whole fortnight — and the margin before anything is unrecoverable is about seven
consecutive misses, set by Yahoo's 55 days at thirty minutes, the shortest reach in the
basket. VIX rides the same commit so the skip-if-fresh check can see the latest close
and avoid re-fetching CBOE's full 1990 file.

**Never:** `data/tremor/metrics/` and `jumps.parquet`. Derived, gitignored, rebuilt in the
run that needs them.

**The Actions cache** (`metrics/`) holds what a warm run extends. It is uploaded only when
the run actually changed it, plus once a day regardless so a quiet stretch cannot let the
entry age out — GitHub drops a cache nothing has touched for a week, and a cold start
costs every instrument a full rebuild.

### What the store costs

A commit stores the whole shard that changed, so the only number that matters is how much
history shares a shard with the new hour. Settled years get one shard each; the year being
written gets twelve, one per month (`tremor/bars.store_path`). Measured on the real store,
that cuts the bytes rewritten per append from 6.89 MiB to 0.61 MiB — **11.4x** — and
weekly rather than daily commits divide the remainder by seven again. One complete year of
bars for all 61 instruments is 5.2 MiB; recording it costs about 12 MiB of git a year,
against 955 MiB under daily commits of year-sized shards.

---

## When it breaks

The Tremor steps are `continue-on-error` so a fault in the detector cannot take down
delivery — and the job is **failed at the end anyway**. A pipeline that quietly stops
updating while the repository looks healthy is the failure this arrangement exists to
prevent.

If a fetch fails, `pipeline` and `jumps` still run on the bars already stored, so healthy
instruments still get events. Delivery sends a move only within 24 hours of its being
found, so what did not refresh in time is not sent late. Health does not record a clean run or send "recovered" while the Tremor step is
red — empty events would otherwise look like a quiet hour.

A provider failure names the instruments and their providers in a message to
`TELEGRAM_HEALTH_CHAT_ID`. The provider is not switched automatically. A Yahoo or Tiingo
rate limit that survives retries skips that provider's remaining instruments and is named
in the same message; a 404 stays a per-instrument dark.

**A fetch is retried three times before it counts as a failure** — two seconds of backoff
then four, inside each provider's client. That holds for all three providers on the
hourly path: Tiingo, Yahoo and Coinbase. A dropped connection therefore costs six seconds
rather than a red run, against a twenty-minute job timeout, and a failure that reaches the
health message is one that survived all three attempts. Twelve Data waits 8 and 16
instead, because a retry there spends a credit against an 8-a-minute plan; it answers
deepening and gap-fill runs rather than the hourly one.

**Three things watch, and none of them needs code here:**

- **cron-job.org** emails when it cannot reach GitHub — the HTTP call failed.
- **GitHub** emails on a failed workflow run — the call succeeded, the job did not.
- **the health check** counts consecutive failed runs and says so in
  `TELEGRAM_HEALTH_CHAT_ID`.

The three cover different things, and the division is deliberate. The health check can
only report runs that FAILED: it lives inside the run, so a run that never happened
cannot increment anything. **That case belongs to cron-job.org**, which is the only party
positioned to notice it — the trigger is its call to make, so it is the one that knows
the call stopped being made, and it emails when it cannot get through. Nothing inside
this repository can do better, because anything that lives in the run shares a failure
mode with the run.

### What is deleted, and what is corrected

The bot posts to a **public channel** and is an administrator there: it can edit its own
messages at any age and delete any message. For the week of the open note every run brings
the channel in line with the events table (`architecture.md`, "The week"):

- **deleted** — an event that is gone (its push, or its row's ping); an event that turned
  rarer inside its 24 hours, which then goes out again and rings; a note part no longer
  needed; every ping of the week when the next note opens;
- **edited** — everything else that changes: a push whose numbers moved or whose word
  fell (⬜ at `noticeable`), the note, a ping whose numbers moved, and — after an event's
  24 hours — a row's ping that becomes the push. An edited or re-sent event says why on
  its `✏️` line.

Nothing of an earlier week is touched. **A detector update** (a new
`jumps.detector_version()`) deletes every push and ping of the current week, keeps the note
and the calendar, and carries on with what is found from that run on — expect that on the
first run after a change to the detector or the basket. A delete Telegram refuses is struck
through by an edit instead, and Telegram's reason is logged.

---

## Silence is the normal state

About 9 pushes a week across today's 61 instruments and about 16 note rows, each with a
small ping; the busiest week of the last year had 32 pushes and 52 rows (`decisions.md`).
One note a week opens at the first run after the week's last NYSE close — normally Friday
16:05 New York, 20:05 UTC in summer and 21:05 in winter; Thursday on a Good Friday week,
13:05 on a half day — and fills as moves are found; it opens even when nothing has happened
yet ("Nothing so far"). Every message counts down on its time line to the funds' close its
move is checked at, one silent edit an hour, and then shows how much of the move held.

The economic-calendar forecast goes out once a week, in the same run immediately before
the note opens — so the note, the message that keeps changing, is the last one in the chat.
It takes its day from `tremor.routing` rather than a weekday of its own, so the pair cannot
be separated. It covers Monday 00:00 UTC to the following Monday, read from the archive
rather than the live feed, and consecutive digests abut exactly.

The calendar archive is topped up from the live feed once a day, separately from the
weekly digest: pushes name the releases in the three hours around a move on any day of the
week, and a schedule fetched last Friday does not have the speech added on Wednesday.

**Nothing rings more than 24 hours after its event's first move was found.** This is load-bearing
rather than tidy: the events table holds the whole history, so without it the first run
after a mute would deliver years of alerts at once. The note opens at the first run of its
week; the calendar goes out only within four hours of the opening, so after a longer outage
the note opens with an empty calendar that says there was an outage — as it does when the
calendar source does not reach the end of the week. An empty events table changes nothing
at all, the note included — it cannot tell "nothing happened" from "the pipeline did not run".

A quiet day is still possible, and is not evidence of a fault. What distinguishes the two is
the Actions tab: green runs mean it looked and found nothing.

---

## Checking on it

```bash
PYTHONPATH=. python tools/stage_report.py   # rates by word, block and channel; the biggest hours
```

Nothing imports that file and no threshold is set from its numbers. The alert count is a
thing to report afterwards, not a thing to steer by.

To run the whole pass by hand:

```bash
export TIINGO_API_KEY=...       # hourly bars
export TWELVEDATA_API_KEY=...   # archive / gap-fill / deepening
export FRED_API_KEY=...         # the VIX series only

python -m tremor.backfill
python -m tremor.pipeline
python -m tremor.jumps
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
