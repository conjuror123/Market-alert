# Why it is built this way

> **Holds** the choices that are easy to second-guess, so they are not re-litigated:
> the rule now in force, and the number that settled it.
> **Does not hold** how it works (`architecture.md`), how to run it (`operations.md`),
> or what is still open (`concerns-for-later.md`).
> **Add an entry when** evidence settles a choice. State the rule, then one line of what
> was measured. Not the alternatives, not the story.

---

## The jump detector

**Copy an established detector rather than design one.** The hand-made ladder asked its
reader too many questions — three tables of rungs, a rate per rung, a sensitivity — none of
which could be answered from anything but taste. The problem this bot solves already has a
standard answer: the intraday jump test of Lee & Mykland (2008, *Review of Financial
Studies* 21:6), with the time-of-day correction of Boudt, Croux & Laurent (2011) and the
group ("co-jump") test of Bollerslev, Law & Tauchen (2008). It is built in stages, each
with its source and a measured before/after, so what a message means can be followed.

**Stage 0 is two rules and nothing else.** The hour's move over the instrument's bipower
volatility, and a word from the size of that ratio. One-a-day, channels, the date, the held
check, the time-of-day scale, the gap, blocks, the own move and the floor all come back as
their own stages.

**Bipower, not a standard deviation.** Lee & Mykland's eq. 8 averages products of
neighbouring moves, so a jump inside the window pairs with ordinary moves on either side
and never with itself; with squares, one 0.05 jump among 0.001 moves inflates the yardstick
more than 1.5x (pinned in `tests/test_tremor_jumps.py`).

**Half a year of calendar time, the same for every instrument.** The paper's valid window
runs from √(252·n) to 252·n bars (n bars a day): a volatility estimate must hold enough
bars that one jump inside it barely moves it, and stay small enough to be local. The paper
picks the smallest valid value only because, in its simulations, a larger one "only
elevates the computational burden". What the window has to follow is the volatility
regime, which runs on the world's calendar rather than on a market's opening hours, so the
same calendar span is used everywhere; half a year gives a fund about 880 bars, a currency
pair 3,130 and a coin 4,380, all inside the range. Measured, a longer window cuts flags
modestly (about 10% at 30 days against the minimum) and the choice of 3 or 6 days changes
little.

**A young series is scored from the paper's minimum, not after a half-year warm-up.**
Thirteen funds' records begin on 2020-02-10; a strict warm-up would have left them blind
through March 2020. Scoring starts at 42 bars (a fund) or 78 (a 24-hour market) and the
window grows; those rows are marked `young` and reported apart.

**The words are √2 apart, half an earthquake magnitude.** Measured on the basket, doubling
a move's size (in half-year sigmas) makes it about ten times rarer — a Gutenberg–Richter
law with a slope near one — so a √2 step makes each word about three times rarer than the
one below. The bottom is 3.9σ for now; the exact 99% level is 3.2σ for a fund's day and
3.5σ for a 24-hour day, and the choice between them is the last stage, once the flags mean
what they should.

**What the papers say about using what we know of an asset.** Model known repeating
patterns — the time of day (Boudt, Croux & Laurent find it recovers small jumps in quiet
hours and removes false ones in busy hours; without it, half or more of detected jumps can
be spurious). Do not filter news: about a third of FX jumps coincide with US data releases
(Lahaye, Laurent & Neely), and they are the real thing — label them. Fat tails are what a
jump test detects, so a per-asset tail shape is a risk-management idea, kept as an
optional later stage.

**The gap: each kind against its own kind, over the same half-year.** Every reading of an
instrument — hours, nights, weekends — is read against the same half-year of the world's
events. Not by clock time: a weekend is 65 hours and a night 17.5, yet a weekend's gap is
only slightly bigger (French & Roll 1986; 1.17x on our funds), because prices move far less
while markets are shut. Measured on our history, each candidate yardstick predicting the
next gap (QLIKE, lower is better):

| | own kind, half-year | nights and weekends pooled | hourly σ × a measured ratio | own kind, 2 years |
|---|---|---|---|---|
| fund nights | **1.65** | **1.63** | 1.95 | 1.78 |
| fund weekends | 2.10 | 1.94 | 2.34 | 1.99 |
| FX weekends | 3.90 | — | 5.59 | 2.96 |

Pooling nights with weekends and longer weekend windows score a little better; separate
kinds were kept anyway, because a currency pair has only weekends and so needs a weekend
yardstick of its own regardless, and one rule for both calendars reads more simply than a
small gain. The price is precision: 26 weekends a half-year (the paper's minimum for weekly
data is 7) leave that yardstick uncertain by about ±16%, so weekend words near a boundary
are the least certain. Borrowing from the trading hours loses everywhere: the night does
not follow the day's mood closely enough.

**One event a day, unless the day grows.** A storm of hours is one story, so an instrument's
day keeps its first event and then only readings that reach a rarer word; the same or a
milder word the same day is dropped. A rise is kept because it is new information — a day
that opens `noticeable` and turns `major` is a different day from one that stays
`noticeable`. Over the record this cuts 28,095 flags to 22,168 events, 1,422 of them rises.

**`high` and up push; `noticeable` goes into the note.** The reader's choice, to try: more
messages than the previous detector's one a week was the point of the change. For today's
basket that is about 10 pushes a week and about 19 note rows a week, each row with its own
small ping; the busiest week of the last year had 37 pushes and 61 rows. `noticeable`
alone is two thirds of the events, so it is the one word worth reading in a batch; `high` (5.5σ) is where a move stops being routine for its instrument.
To be tuned with the threshold at stage 12, against a few weeks of real messages.

**One note a week.** With every word from `high` up pushed, the note holds only
`noticeable` rows, and one note a week holds them (it used to be two, Monday and Saturday).
The economic calendar stays a message of its own, sent in the same run just before the note
opens: one is a forecast, the other a report, and they are read differently. Saturday, so
the forecast arrives before the week it forecasts.

**A jump message claims only what the detector measured.** The square's colour is the word,
so the word is not written out; the size is `|move| / σ` to one decimal, `11.0×σ`, at the end
of the first line, in the ping as in the push. Hour or gap is not spelled out as a yardstick
("its usual weekend gap"); the "biggest since" line (stage 3) will say which it was. Dates
read `24.09.2026`. No "biggest since" date, no held-at-close line, no
block/own split until their stages exist — the previous detector's wording for those would
describe a measurement the jump detector does not make.

**What the jump detector replaces.** The ladder of the previous detector: three per-block
tables of rungs (`BLOCK_SIGMA`, `BLOCK_RESID_SIGMA`, `BLOCK_MOVE_SIGMA`) and a
`sensitivity` multiplier. Before that the rungs were ranks within a six-year window, retired
because after a crash nothing could reach the top rung until the crash rolled out (395
moves larger than a typical `extreme` went out milder); before that, fitted tails, which
put SPY's once-in-six-years level anywhere from 2.22% to 6.78% on different windows. A
per-instrument "N times a year" rung was built and rejected: it equalised rates but let
the biggest crisis hours go out as digest rows (70.8% → 61.6% of the biggest hours pushed),
and its settings were still choices rather than derivations. Its code, and its decisions -
the block residual, the ladder, the record book, the check-ins, the report card - are on
the production branch (`claude/price-spike-monitoring-app-yyg2jg`, this file's copy there);
a stage that brings a piece back brings its entry with it.

---

## Delivery

**Rarity and urgency are different questions.** Rarity belongs to the instrument and means
the same whether five are watched or fifty; willingness to be interrupted belongs to the
person and does not grow with the watchlist. Keeping them apart is what stops the alert
rate tripling the day three instruments are added.

**The system does not count its own alerts.** No weekly cap. A detector that goes quiet on
the third alert of the week fails adversarially: the week the franc is unpegged is exactly
the week records cluster. Volume is controlled where it is generated.

**A closed note is a record, not a feed.** Every tracked note is re-rendered each run, so a
late event can appear — but past `DIGEST_GROW_AFTER_CLOSE_HOURS` it may be corrected and
may not grow. Without that bound one cold rebuild grew a note that had closed two days
earlier from 5 rows to 19 and posted the difference as two alerts at breakfast. The rows
were right; the interruption was not.

**Delete pings and a day's lower messages; correct everything else.** The reader's rule. A
ping is a throwaway pointer at the note, and a day that grew has one story, told by its
rarest message — so those go. Any other message that turns out wrong is corrected where it
stands, by an edit: a deleted alert looks to a reader like one that never happened, and an
edited one says what really happened. The channel is public and the bot an admin, so
Telegram lets it do either at any age.

**The size is said in σ, and the word is the colour.** `|move| / σ` over the instrument's own
half-year, to one decimal, at the end of the first line — `11.0×σ` — in the push and the
ping alike. A bare percentage says nothing across a basket where short Treasuries move
0.024% in a usual hour and a coin several per cent; the σ says how far out the move is for
that instrument. The word is not written: the square already says it.

**Recall on the obvious is the yardstick, not precision.** Of each instrument's biggest
hours (its top 0.01%), how many were flagged and at which word — `tools/stage_report.py`
prints it for every stage. Precision is the wrong measure here: delaying every alert by six
hours raised it, from 52.0% to 56.0%, which no measure of a detector's quality should do.

---

## The data

**The store is unadjusted, with ex-dates recorded.** Adjusted series are recomputed
retroactively on every dividend, so a history built from them changes underneath every
yardstick measured on it. The ex-dividend drop happens between sessions, and nothing between
sessions is a return here.

**Corporate actions are declared, not inferred.** `divCash` and `splitFactor` come from
Tiingo's daily endpoint. Inferring a step from the ratio of adjusted to unadjusted cannot
see a split at all — both series are split-adjusted, so it cancels — and a vendor dividing
a nominal pre-split dividend by a split-adjusted price reports every earlier step at twice
its size. Splits are recorded but **excluded** from un-adjustment: the store is already
split-adjusted.

**The newest rows of the metrics store are never trusted.** The run fires five minutes past
the hour and stores two to thirteen per cent of that hour's volume. Bars heal on the next
fetch; metrics did not, so every hour was judged on its first five minutes — 2026-09-16
18:00, the FOMC statement, went in as SHY +0.02% when the hour closed at -0.19%, taking
thirteen instruments' events with it. `extend_asset_metrics` re-scores its last two days
rather than trusting them, which costs nothing.

**The shard being appended to is the only one whose size matters.** Git cannot delta
parquet, so recording one hour costs the size of the shard it lands in. Sharding by year
does not fix it — the live year's shard grows all year, making the annual bill 183 times
one complete year: 955 MiB to record 5.2 MiB of bars. Settled years keep one shard each and
the live year is split by month. Which year is live is read off the data, not the clock.

**A day is not a unit of completeness.** Gap detection asks about hours: a day present with
three of its seven hours is a hole a day-level check cannot see.

**One provider per instrument, chosen by measurement.** Each candidate was compared against
the stored bars hour by hour in basis points, against one sigma of an hourly move (20-40
bps). A feed disagreeing by a few basis points on a thin fund is not a cheaper feed — it is
a source of alerts for moves that did not happen.

---

## The shape of the repository

**Derived data is not tracked.** The metrics and the event table are rewritten every run
and rebuild from the bars in about fifteen seconds.

**Bars commit once a day, not hourly.** Appending to Parquet leaves earlier row groups
byte-identical, so a day of new bars costs about 1 MB; hourly commits would be twenty-four
times that for the same information.

**Two stores, not one.** Parquet for columnar history, JSON for state where a whole-file
rewrite is the point. Nothing here needs a server.

---

## Settled and closed

Raised, dealt with, and not to be raised again.
- **Retention as a gate** — refused. Gating buys silence for as long as the answer takes.
- **A watchdog for the trigger's silence** — not this repository's job. cron-job.org makes
  the call, so it is the party that knows the call stopped; anything inside the run shares
  a failure mode with the run.
- **`price_monitor/fxcm.py`** — deleted. Covered seven of eight pairs and spliced history
  blind where Dukascopy gates on a measured overlap.
- **`tremor/volume.py` and the `v_r` column** — deleted. Computed in every metrics build
  and read by nothing.
- **VIX refetched from 1990 every run** — fixed. The stored parquet is read first.
- **FX fetched into a closed market** — fixed. The skip guard takes its expectation from
  the same session walk the bar loop uses.
- **Health messages in the product channel** — fixed. `TELEGRAM_HEALTH_CHAT_ID`.
- **`schema/event_export.schema.json`** — deleted, with its `jsonschema` dependency.
  Nothing produced the export and no test validated it, despite a comment saying one did.
- **The previous detector** — deleted from this branch, with its tests, tools and
  workflow: the block residual and BMP, the per-block ladder tables and the size floor, the
  record book, the check-ins and retention, the block events, the rate line, the overnight
  gap's own scoring, the report card. It keeps running production from its own branch until
  the switch, and its code is read there when a stage needs a piece of it. The jump events
  were identical before and after: 22,840, of them 7,907 pushes.
