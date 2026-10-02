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
| Live fetch (all providers; Twelve Data's one batch runs alongside) | ~15 s |
| Metrics and events, warm run | ~5 s |
| Metrics and events, cold rebuild | ~15 s |
| Whole job, median | ~2 min |
| Our own timeout | 20 min |
| GitHub's hard cap | 6 hours |

No live fetch waits on a rate limit. Tiingo answers in about 0.28 s with no enforced
throttle; Yahoo and Binance have no pacing requirement at this volume. A `config_version` mismatch
forces a cold rebuild of the metrics (about ten seconds locally) — that is the guard
working, not a fault. The jump detector rescores the whole history every run, in about three
seconds, so it has no warm state to lose.

---

## Quotas

| | Limit | Used |
|---|---:|---:|
| **Tiingo** | 50/hour, 1000/day | 27 funds — 54% of the hour |
| **Alpaca** | 200/min, IEX live free; SIP to 15 min back | 30 funds — 30 requests a run |
| **Twelve Data** | 800/day, 8/min | 8 funds — one batched request a run, in the background; ~65 credits a day; plus archive and gap-fill |
| **SiftingIO** | 10,000/**month**, a few a second | 17 FX pairs — about 8,500 a month (85%); USD/BRL only in its B3 session |
| **Sina Finance** | none published; wants a Referer | 33 funds (half-hour bars) and the LME's tin, nickel and aluminium |
| **Yahoo** | none published | 34 funds, 4 futures and the dividend check |
| **Google Finance** | none — a web page, read against its terms | TUR, one page a run |
| **Yahoo futures** | as Yahoo | 4 commodities, one request each a run in session; live from the front contract (`tremor.futures.front_contract`) |
| **Binance** | no key; 6,000 request weight a minute per address | 16 coins, one request each a run |
| GitHub Actions minutes | unlimited (public repo) | — |
| Repository size | 1 GB warning, ~5 GB cutoff | 636 MiB packed (2026-10-02) |

SiftingIO's monthly bucket and Twelve Data's eight a minute are the binding live limits:
each FX pair costs about 520 SiftingIO calls a month, one per hour of the FX week,  and
Twelve Data allows eight symbols a minute, so its funds go eight to a request, a minute
apart, in a thread started before the other providers are asked
(`tremor.backfill.fetch_twelvedata_live`): the wait overlaps their work instead of adding
to it. The US-session funds skip when the NYSE calendar says no bar can have appeared since the newest
stored one; the 8 FX pairs skip when the Sun 17:00 → Fri 17:00 New York week is shut
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

**Every run, but almost always nothing: settled months of `data/tremor/bars/`.** A month
enters git once, as `YYYY-MM.csv.gz`, on the first run at least a week after it ends,
and is never rewritten (`tremor/bars.py`): about 0.7 MB a month for 173 instruments.

**Never in git: the open months** (`YYYY-MM.csv`, gitignored). They are one archive,
`bars-live-<run>.tar.gz`, on the prerelease `bars-live-<branch>` of this repository
(`tools/hot_bars.sh`). Each run restores it before the backfill and saves it straight
after, uploading the new archive before deleting the old, so a save that dies leaves the
previous one. A release that exists but cannot be read fails the run before anything is
scored: a store missing its last weeks would read every recent move as gone. A save that
fails loses that run's bars only until the next run, which fetches from the newest bar the
archive holds. The first run with no release keeps the open CSVs the checkout still has
from before this layout, creates the release, and takes them out of git.

**Saturday 04:00 UTC only:** `data/tremor/vix/`, one Parquet file rewritten daily, so the
skip-if-fresh check can see a recent close and avoid re-fetching CBOE's full 1990 file.

**Never:** `data/tremor/metrics/` and `jumps.parquet`. Derived, gitignored, rebuilt in the
run that needs them.

**The Actions cache** (`metrics/`) holds what a warm run extends. It is uploaded only when
the run actually changed it, plus once a day regardless so a quiet stretch cannot let the
entry age out — GitHub drops a cache nothing has touched for a week, and a cold start
costs every instrument a full rebuild.

### What the store costs

Git stores a whole new copy of every file a commit changes, so the bars are laid out to
change committed files as seldom as possible (`tremor/bars.store_path`): years before
2026 one Parquet shard each, as they were; from 2026 one shard per month, settled months
`.csv.gz`, committed once; the open months on the release. About 9 MB of git a year, against
124 MiB for committing the open month's CSV hourly, 18 daily and 51 for weekly Parquet
(simulated on the week of 2026-09-21). The release costs the repository nothing: release
files are not part of it.
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
`TELEGRAM_HEALTH_CHAT_ID`. The provider is not switched automatically. A Yahoo, Tiingo or
SiftingIO or Alpaca rate limit that survives retries skips that provider's remaining instruments
and is named in the same message; a 404 stays a per-instrument dark.

**A fetch is retried three times before it counts as a failure** — two seconds of backoff
then four, inside each provider's client. That holds for every provider on the hourly path
but one: Tiingo, Alpaca, SiftingIO, Sina, Yahoo, Google and Binance. A dropped
connection therefore costs six seconds rather than a red run, against a twenty-minute job
timeout, and a failure that reaches the health message is one that survived all three
attempts. Twelve Data's hourly batch is retried twice, 61 seconds apart, because a retry
spends the minute's eight credits; it runs on its own thread, so the wait is not added to
the run. Its deepening and gap-fill requests wait 8 and 16.

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

About 22 pushes a week across today's 173 instruments and about 49 note rows, each with a
small ping, over the year to 2026-10-01; the busiest week had 87 pushes and 142 rows
(`decisions.md`).
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
export TIINGO_API_KEY=...       # hourly bars: 27 funds
export ALPACA_KEY_ID=...        # hourly bars: 30 funds (IEX); history (SIP)
export ALPACA_SECRET_KEY=...
export SIFTING_API_KEY=...      # hourly bars: FX
export TWELVEDATA_API_KEY=...   # hourly bars: 8 funds; archive / gap-fill / deepening
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
