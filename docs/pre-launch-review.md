# Pre-launch review

A fresh read of the whole system before notifications are switched on, September 2026.
The detector measures well (`tools/report_card.py`: nothing missed at the top of any
instrument's own distribution, 0.008% firing at the bottom, 22.5 pushes a year). What
follows is everything found that is NOT the detector.

---

## 1. FATAL — Tremor does not run in production

The system is not wired to anything.

* `.github/workflows/price-monitor.yml` runs `python -m price_monitor`, the older
  per-asset detector. Nothing in CI runs `tremor.bars`, `tremor.pipeline`,
  `tremor.cross_section` or `tremor.saed`.
* Its commit step saves `state.json`, `alerts_log.json`, `candle_history/`,
  `decision_log/` and `economic_calendar/`. **Not `data/tremor/`.**
* `price_monitor.tremor_delivery.load_events` reads `data/tremor/saed_events.parquet` off
  disk — a file only ever written by a hand-run of the pipeline and committed by hand.
* `STALE_AFTER_HOURS = 48` refuses to send anything older than two days.

The newest event in the committed table is 2026-08-30. **Unmuting sends nothing, silently,
and goes on sending nothing.** The failure is invisible: a system that is correctly quiet
and a system that is broken look identical from the outside.

`backfill-tremor-bars.yml` is `workflow_dispatch` only and describes itself as a one-off.
The README documents the pipeline under "Running it by hand". It was never built to run
itself; that is not a regression, it is a missing half.

**Also: the hourly monitor has been dead since 2026-09-01 13:05** (run #84, the last of
84). The external trigger — cron-job.org calling `workflow_dispatch`, used because GitHub
schedules fire unreliably on low-activity repositories — stopped. Even the old detector is
silent. Nothing alerted anyone, because the health check only reports failed runs, not
absent ones.

**And production runs a different branch**, `claude/price-spike-monitoring-app-yyg2jg`.
This branch is 33 ahead, 0 behind: a clean fast-forward.

### What it costs

Measured on this machine, whole history, twenty-four instruments:

| stage | time |
|---|---:|
| `tremor.bars` | 1s |
| `tremor.pipeline` | 122s |
| `tremor.cross_section` | 15s |
| `tremor.saed` | 135s |
| checkout + pip install | ~40s |
| **per run** | **~5.2 min** |

The existing monitor takes ~45s a run, so ~540 min/month. The free allowance on a private
repository is 2,000 min/month, then $0.006 a Linux minute.

| cadence | minutes/month | cost |
|---|---:|---|
| hourly | 4,284 | ~$14/month over the allowance |
| every three hours | 1,788 | free; a push up to 3h late |
| incremental scoring | far less | engineering, not money |

The pipeline recomputes rolling regressions over twenty-two years to score one new bar.
Scoring only the trailing window is the real fix and turns minutes into seconds; the two
cadences above are what is available without writing it.

### A second problem underneath

`data/tremor/` is 490 MB and `.git` is already 1.7 GB. Committing metrics and residuals
every hour would end the repository. Only `bars/` (the accumulating raw history) and the
two event tables (1.4 MB) need to persist. Everything between them is derived and belongs
in an Actions cache, not in git.

### And a trap in the stage order

Running `tremor.cross_section` on its own **truncates** `metrics_basket_hour.parquet` from
44 columns to 26. The eighteen it drops — `si_total`, `m_calendar`, `m_vix`, `decision`,
every `trigger_*` — are written by `tremor.cluster`, which runs after it and enriches the
same file. Found by re-running the stage while timing it, and restored from git.

So an hourly workflow that runs `bars → pipeline → cross_section → saed` and stops there
would silently strip the columns `evaluate` and `calibrate` need. Either run `cluster`
too, or have `cross_section` merge into the existing file rather than replace it.

---

## 2. The messages will read as broken

Rendered from real events:

```
🚨 Treasuries 1-3 years - biggest move in about three years
     +0.13%, hour to 2026-07-29 17:00 UTC
```

Nobody reads "+0.13%, biggest in three years" as anything but a bug. It is not one: SHY's
median hour is 0.0127%, so this is ten times normal, and across all pushes the median is
**15x that instrument's typical hour**, with only 8% under 5x. But **45% of pushes show a
number under 1%**, and the message never gives the reader the scale that makes the claim
true. One clause — "10x its typical hour" — closes the whole gap.

---

## 3. Checked and clean

The failure modes that quietly ruin systems like this, and what was found:

| | |
|---|---|
| Look-ahead in the ladder | none. `rolling_levels` fits on `magnitude[:start]` and applies from `start` on |
| BMP denominator | leave-one-out, and deliberately better than the published form: including an asset in its own denominator would let a real single-asset move raise the bar it is measured against |
| Unknown retention | treated as "has not taken the test", not "reverted" — the bug that would silence every freshly produced event |
| Dividends and splits | land in the overnight gap channel, which the detector excludes by construction |
| Partial final bar | theoretical only: ETF hours consistently store `n_src = 2`, both half-hour bars present |
| Flood on unmute | `STALE_AFTER_HOURS = 48` bounds it |
| Duplicate sends | `concurrency: group: price-monitor` serialises runs; `sent` is keyed by event id |
| DST | `digest_slot` resolves through `ZoneInfo`, not timestamp arithmetic |
| Reproducibility | re-running the pipeline reproduced every per-asset metric and residual byte-for-byte |

Minor, not blocking: `corporate_actions.csv` is static and will not pick up a new dividend
(low risk — the gap channel is excluded anyway); one push in 484 (TLT, −0.06%) fired below
its own median hour.

---

## 4. Is this the best available method?

Checked rather than assumed.

[A 2025 benchmark of time-series foundation models](https://openreview.net/forum?id=H27kvyG4qf)
finds their anomaly-detection performance "does not significantly differ from simple
one-liner baselines like moving-window variance and squared-difference", and
[Strong Linear Baselines Strike Back](https://arxiv.org/pdf/2602.00672) makes the same
argument from the other side. Classical methods are reported as unbeatable on small
datasets, where deep models overfit.

This system is a strong classical baseline: rolling factor regression, standardised
residual, per-instrument extreme-value ladder, non-parametric confirmation. There is no
machine-learning upgrade being left on the table, and the literature is explicit that
reaching for one would most likely cost complexity for nothing.

The 2026 architecture writing is about Kubernetes, event streaming and sub-millisecond
latency for trading desks — irrelevant at twenty-four instruments on hourly bars. The one
transferable idea, contextual filtering against alert fatigue, is what `routing.collapse`
and the rank gate already do.

---

## Order of work — status

1. ~~Restart the external trigger.~~ Done by hand.
2. ~~Decide the cadence.~~ Moot: the repository is public, so Actions minutes are free
   and unmetered. The run was also taken from 273s to 172s (§45) for its own sake.
3. ~~Build the pipeline into the workflow.~~ Done, `cluster` included. Derived data is
   gitignored rather than cached — it rebuilds from the bars in the same run that needs
   it — and the bar archive and event tables commit once a day rather than hourly.
4. ~~Add the "x its typical hour" clause.~~ Done.
5. **Fast-forward the production branch.** Outstanding, and the only thing between here
   and live.
6. Unmute. `tremor_alerts_muted` is now false. `alerts_muted` is deliberately still
   true: it silences the OLD per-asset detector, which is the thing Tremor replaces, and
   running both would alert twice on the same move.

The Twelve Data budget, raised in §1 as fitting with no headroom, is settled: the
forward fetch now skips a US-equity instrument when the NYSE calendar says no bar can
have appeared since its newest stored one. 504 requests a day becomes 293, and with
price_monitor's 192 the total is 485 of 800. FX is never skipped - it has no session
table here and its Sunday reopen is exactly the edge a hand-written rule would get
wrong. The other half of that saving, price_monitor re-fetching eight pairs Tremor has
already stored, is left for after the system has run: it changes what the incumbent
detector reads, and deploy day is the wrong day for that.
