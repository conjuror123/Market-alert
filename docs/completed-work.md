# Completed work

Issues that were on the open list and are not any more. Kept so that a later reader can
tell "this was considered and dealt with" from "nobody looked at this", and so that a
concern does not get raised a second time.

`docs/decisions.md` records *why* each of these was resolved the way it was. This file
only records *that* they were.

---

## Closed on review

**Retention is a note, not a gate.** Raised as a limitation: a move that fully reverts
still interrupted you, and the message can only say so afterwards. Closed as not an
issue — gating on retention would buy silence for as long as the answer took to arrive,
and a once-in-three-years move that arrives six hours late is a worse product than one
that arrives now and is corrected. This is the intended design, not a gap in it.

**"Nothing notices a silence."** Raised as the one open gap that had actually cost
messages: the health check counts consecutive *failures*, and only a run that executes
can increment one, so the six-day trigger outage in September went unreported. A watchdog
inside the repository was designed and never built. Closed as **not this repository's
job** — the trigger is cron-job.org's call to make, so it is the party that knows the
call stopped being made, and it emails when it cannot get through. Anything living inside
the run shares a failure mode with the run. `docs/operations.md` now describes the three
watchers and the division between them; every trace of the pending-watchdog framing is
gone from the code, the config, the docs and the report card.

---

## Fixed

**`price_monitor/fxcm.py` deleted.** Frozen since roughly week 17 of 2026, covered only
seven of the eight currency pairs (no USD/CNH, so it could not serve the basket at all),
and spliced history *blind* where the Dukascopy path gates on a measured overlap.
Dukascopy supersedes it on depth, coverage and safety. No capability lost.

**`tremor/volume.py` and the `v_r` column deleted.** 111 lines producing a column in
every metrics build that nothing read — delivery hardcoded `volume_z=0.0`. The
data-quality check that uses `has_volume` is unrelated and stays.

**The VIX is no longer refetched from 1990 on every run.** The stored parquet is read
first, and both fetches and the parquet write are skipped when what is stored already
covers what can exist. This also stopped a daily commit of an unchanged file.

**FX is no longer asked into a closed market.** The skip guard exempted every
non-`us_equity` template, so the eight currency pairs were fetched 24 times a day
including all weekend, against 47 week-hours that never carry a bar. The guard now takes
its expectation from the same session walk the bar loop uses, so the skip cannot disagree
with what counts as a bar.

**Health and provider failures go to a private chat.** `TELEGRAM_HEALTH_CHAT_ID` is wired
through `price-monitor.yml` and `reconcile.yml`, falling back to the main chat only while
the secret is unset. No health or diagnostic message reaches the public channel.

**Dividend steps come from declared amounts, not inferred ones.** The old deriver read a
step out of the ratio of adjusted to raw close, and the provider divides the nominal
pre-split dividend by the split-adjusted price — so every pre-2025-12-05 step on
XLK/XLY/XLE/XLU/XLB read exactly twice its true size, compounding into the 504–2,705 bp
error that made the archive import fail alignment. Tiingo's declared `divCash` and
`splitFactor` replace the inference. Splits are recorded but excluded from
un-adjustment: the store is unadjusted for dividends only and is already split-adjusted,
so feeding a 2.0 into the cumulative product would have manufactured a 100% error on
every older bar.

**`data/tremor/evaluation.md` marked frozen.** Its generator would now silently overwrite
it with a different report, scored against a yardstick neither detector claims to answer.
The file carries a "do not regenerate" banner.

**The five-minute hour.** The job runs five minutes past each hour and was scoring the
hour it stood in — a few per cent of the trading that would eventually happen — then
never looking again once the hour closed. `extend_asset_metrics` now re-scores its last
`RECOMPUTE_TAIL_BARS` rows instead of trusting them.

**`sigma_LT` is exponentially weighted, with a per-calendar half-life.** The box window
counted a bar from two years ago exactly as much as this morning's, and a bar one hour
older not at all — an edge that travelled through the data and stepped the yardstick 16.8%
on a twenty-sigma bar. One bar count also meant 290 calendar days of memory for an ETF
and 58 for a coin. Both fixed; see `docs/decisions.md`.

**A day's close is no longer read off a day that is still open.** Retention took the
newest bar in the store as the day's last, so a move at 01:00 was reported as "still
there at this day's close" three hours later, with twenty-one hours of that day still to
run.

**The citations to the deleted specification are gone.** 299 references to section
numbers of a document that no longer exists, across 46 files, plus the prose that pointed
at it and the nineteen in the published event schema.

**The gap channel is gone.** `r_gap` — the overnight jump between one session's close
and the next session's open — was computed on every bar, written into every metrics
table, and read by nothing. So was `gap_masked`, the flag marking ex-dates so the gap
channel's distribution would not be skewed by dividend steps. Behind them sat
`corporate_actions.load_actions()`, fetched and threaded through four levels of
`pipeline.py` for the sole purpose of feeding a flag on a column nobody read; it is
deleted too. **The split itself stays** and is what matters: the first bar of a session is
measured from its own open, so an overnight jump — dividend, news, another venue — is
never a return. `load_steps()` is untouched: un-adjustment is a different question and a
live one.

**The tier frequencies are measured, not asserted — and the argument behind them was
retracted.** `README.md`, `docs/architecture.md`, `docs/decisions.md` and the report card
all described the four rungs as return periods: monthly, quarterly, every three years,
every six. `docs/decisions.md` went further and explained *why* that was exact, using the
1/N argument — the probability that the newest of N observations is the largest of those
N is 1/N, so "the largest in the trailing six years" happens about once in six years
because there is no model to be wrong.

That argument is sound, and it belonged to the **rank rule**, which was retired in favour
of size in sigma — monotone, so a crash cannot put later moves in its shadow, but *not*
frequency-calibrated by construction. The docs were defending a mechanism that had
stopped running. Measured over the archive, per instrument: every 2 months, 6 months, 17
months and 2.9 years — the two quiet rungs about half as often as the wording promised,
**the two that interrupt about twice as often.**

`tools/dashboard.py` now derives the rate per instrument-year, with each instrument's own
span as the denominator rather than the whole archive (half the basket is younger than
the archive), and the report card reads it out of the event table instead of repeating
the adjectives. The other three documents describe the rungs as what they are.

**Run health is measured by a tool, and the page says what it measures.** The report
card's reliability figures came from a throwaway script, and its caption described a
measure — "share of that day's runs that failed" — that scored the worst outage in the
record as a perfect zero, because an outage produces no failed runs. `tools/run_health.py`
counts hourly slots with no successful run, and drops partial days at the edge of the
window rather than scoring them as outages.
