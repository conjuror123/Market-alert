# Why it is built this way

> **Holds** the choices that are easy to second-guess, so they are not re-litigated:
> the rule now in force, and the number that settled it.
> **Does not hold** how it works (`architecture.md`), how to run it (`operations.md`),
> or what is still open (`concerns-for-later.md`).
> **Add an entry when** evidence settles a choice. State the rule, then one line of what
> was measured. Not the alternatives, not the story.

---

## The jump detector (replacing the ladder, in stages)

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
messages than the running detector's one a week was the point of the change. For today's
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
block/own split until their stages exist — the running detector's wording for those would
describe a measurement the jump detector does not make.

**What the jump detector replaces.** The ladder of the running detector: three per-block
tables of rungs (`BLOCK_SIGMA`, `BLOCK_RESID_SIGMA`, `BLOCK_MOVE_SIGMA`) and a
`sensitivity` multiplier. Before that the rungs were ranks within a six-year window, retired
because after a crash nothing could reach the top rung until the crash rolled out (395
moves larger than a typical `extreme` went out milder); before that, fitted tails, which
put SPY's once-in-six-years level anywhere from 2.22% to 6.78% on different windows. A
per-instrument "N times a year" rung was built and rejected: it equalised rates but let
the biggest crisis hours go out as digest rows (70.8% → 61.6% of the biggest hours pushed),
and its settings were still choices rather than derivations. The tables stay in the code
only until the switch, because the running detector reads them.

---

## The residual

**The market model, with an estimation gap, through zero.** `r = beta*F + e`, a rolling
500-bar regression ending three bars before the bar being judged. Three bars because the
leak is short and three of five hundred does not measurably move the coefficients. No
intercept: 500 hours cannot measure a drift of about 0.004% an hour, so subtracting one only
added noise (events 9,690 → 9,704, pushes unchanged).

**The regressor is the instrument's own block factor**, oriented by sign — within FX the
dollar-quoted and dollar-based pairs otherwise cancel and the factor reads near zero. A
basket-wide factor does not scale to sixty instruments the way a block factor does.

**Leave-one-out in the BMP denominator**, which the published form does not do. Each
instrument is tested against its peers individually, so without it a genuine single-asset
move inflates the spread it is measured against and hides itself.

**It is a t, not a z.** Dividing by a sample spread over as few as five peers makes a t;
Wallace's transform maps it to z for the degrees of freedom actually present.

**One size floor, not a rank test.** An unexplained move can be tiny — a residual its
own history finds remarkable in an instrument that ticked +0.03%. The raw move must be at
least twice the instrument's usual hour (`min_move_sigma: 2.0`). It replaced a one-hour
floor plus Corrado's rank test, which did the same job twice: the same messages, 97% of
pushes identical, held 73.9% against 74.2%. The two-tick check went with it (it fired once
in 25 years, and the floor covers it), and so did Patell's correction on the regression
(99.4% of pushes identical).

**The opening is judged against openings.** A US fund's residual is divided by that hour's
usual size relative to all hours, learned per fund over 500 sessions; the opening's
crossing rate fell from 0.61% to 0.35%. Not for FX, whose busy hour is the news itself.

**Peer count is not the discriminator.** Fewer than ten peers is 37.5% of scored hours —
the shape of a 24-hour basket whose equities trade six and a half. The thin hours hold the
best calls (the franc unpeg, Brexit, post-Fukushima), all with nine peers or fewer. What
matters is whether the peers were *moving*.

---

## What counts as an event

**An event carries one bar's numbers.** Escalating inside its day keeps the higher tier
*and* that bar's move — otherwise a push reads "biggest move in 3 years, +0.01%".

**The peak moves at the same tier.** `extreme` is the top of the ladder, so a
strictly-higher rule can never fire for it: USD/CHF opened `extreme` at -3.5% on
2015-01-15 and did -10.5% an hour later.

---

## Who gets interrupted

**Rarity and urgency are different questions.** Rarity belongs to the instrument and means
the same whether five are watched or fifty; willingness to be interrupted belongs to the
person and does not grow with the watchlist. Keeping them apart is what stops the alert
rate tripling the day three instruments are added.

**One event, one interruption, one day.** A calendar day rather than a rolling window, so
the reader can say when the next one can come. The UTC boundary because 00:00 UTC is the
quietest hour there is; a local midnight lands mid-American-session.

**Both push tiers go out immediately, and are edited afterwards.** Of events still standing
at their own day's close, 80% were still standing at the next, against 26% of those that
had already given it back — so waiting is informative, but a once-in-three-years move that
arrives six hours late is worse than one that arrives now and is corrected. Retention
decides what a message *says*, not whether it is sent.

**A close reading waits for the close.** "This day's close" is the last bar the instrument
trades that day, and a live store always ends mid-day — so the reading was taken from
whatever bar had just been fetched, and AVAX-USD read "still there" three hours into a day
with twenty-one hours left. The frame cannot tell a finished day from a three-hour-old one;
`sessions.day_is_closed` can, and both readings wait for it.

**A line about the calendar is not a check-in.** A move made in its closing hour has no day
left to hold through, so its ratio is one by construction. The line is omitted rather than
answered in words.

**The sigma window is a dial between accuracy and crisis loudness, not an estimate.**
Scored as a forecast, every instrument wants the shortest window offered — the wrong
question, since `sigma_eff` is already the fast estimator. Scored against a centred
hindsight estimate, the optimum moves with the bandwidth chosen for "local", so it measures
the choice. The two criteria left disagree because they are one quantity with the sign
flipped: a short window keeps the multiple comparable across eras and goes quiet in a
crash, a long one is the reverse. The setting sits on the loud side, which is the right
choice here and had never been made.

**Exponential weights, not a box.** A box counts a bar from two years ago as much as this
morning's and one an hour older not at all; that edge travels through the data and stepped
the yardstick 16.8% on a twenty-sigma bar. At matched loudness the exponential form holds
the multiple steadier era to era on 82% of equity settings and 65% of crypto ones. It is
cut off at six half-lives, where the measurement stops improving, and normalised by the
weights actually used — so a warm run still reproduces a cold one exactly.

**The half-life is per trading calendar.** One bar count meant 290 calendar days of memory
for an ETF and 58 for a coin, a five-fold spread that fell out of exchange hours. 600 bars
for `us_equity`, 1,400 for `fx_continuous`, 2,000 for `crypto_24_7`, 83 for a daily series
— 86, 82, 83 and 83 trading days, the band the volatility literature settles on. Equity's
worst-year spread of the rarest-1% marker goes 3.02x to 2.00x; crypto gains six points of
coverage in its own worst weeks. The floor cannot exceed the span, or the count inside the
window never reaches it.

**Crypto's window is not extended to match the ETFs' calendar span.** Volatility half-lives
are ~97 days for crypto and 122 for equity, so a flat bar count does give the ETFs more
memory — but crypto's storm coverage saturates at the current span (42% at 5,000, 8,000,
13,000 and 20,000 alike) and era drift gets *worse* for seven of the nine coins, which is
the thing a longer window was meant to fix.

**The system does not count its own alerts.** No weekly cap. A detector that goes quiet on
the third alert of the week fails adversarially: the week the franc is unpegged is exactly
the week records cluster. Volume is controlled where it is generated.

**A closed note is a record, not a feed.** Every tracked note is re-rendered each run, so a
late event can appear — but past `DIGEST_GROW_AFTER_CLOSE_HOURS` it may be corrected and
may not grow. Without that bound one cold rebuild grew a note that had closed two days
earlier from 5 rows to 19 and posted the difference as two alerts at breakfast. The rows
were right; the interruption was not.

**Say what the move was big compared with.** 45% of pushes carry a number under 1%, and
"+0.13%, biggest move in about three years" reads as a bug — short Treasuries move 0.024%
in a usual hour. Both numbers are shown, so the claim is checkable.

---

## How it is scored

**Precision is the wrong yardstick.** Delaying every alert by six hours *raises* it, from
52.0% to 56.0%. No predictive measure can behave that way.

**Recall on the obvious is the right one.** Of the hours in the top 0.01% of an
instrument's own distribution, how many reached the reader? Its counterpart — how often a
below-median hour fires — should be 0%. Neither needs episode labels or an arguable
threshold. Current figures are in `README.md`.

**Judge each event on the quantity its own ladder scores.** `absolute` against the raw
return, `abnormal` against the standardised residual. Swapping them scores 3.5% and 1.1%
and means only that they were swapped.

**Recall is per episode, not per hour.** One shock spans several bars and the detector
reports the peak, so a per-hour figure would mostly measure the debounce.

---

## The data

**The store is unadjusted, with ex-dates recorded.** Adjusted series are recomputed
retroactively on every dividend, so a history built from them changes underneath the record
the ladder is made of. The ex-dividend drop happens between sessions, and nothing between
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

**Derived data is not tracked.** Metrics, residuals, the event table and the record book
are several hundred megabytes rewritten every run and rebuild from the bars in about a
minute and a half.

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
- **The overnight gap** — scored again, on a path of its own (`tremor/gaps.py`), against
  the usual gap after the same kind of close; a midweek holiday counts as a weeknight,
  because two or three a year are too few to learn their own size and borrowing the
  weekend's made them fire three times their share. Unscored when the day's dividend isn't
  confirmed, which is why `tremor.backfill` asks Yahoo for payouts each morning.
- **Stages nothing read** — deleted: the reversion fit, the Q95/Q99 thresholds, the price
  z-score and the basket statistics built on it. Removing them left the events identical.
- **VIX refetched from 1990 every run** — fixed. The stored parquet is read first.
- **FX fetched into a closed market** — fixed. The skip guard takes its expectation from
  the same session walk the bar loop uses.
- **Health messages in the product channel** — fixed. `TELEGRAM_HEALTH_CHAT_ID`.
- **`data/tremor/evaluation.md` and its entry point** — deleted. It scored the SI-Index
  cluster channel, not the delivered detector, against a forecasting label neither claims
  to answer. `tremor/evaluate.py` went with it: its episode and cooldown helpers had no
  caller outside their own tests, because `saed_score` carries its own. Three constants
  in `tremor/windows.py` are now unreferenced and stay there, marked — removing them
  moves `config_version` and rebuilds every metric cold for no change in behaviour.
- **The block-alert aggregator** — deleted. It grouped events by block and hour, wrote
  `saed_block_alerts.parquet` and stamped `aggregate_alert_id` on every event, and nothing
  read either one in the repository's whole history. Its one real use would be folding
  simultaneous pushes into one message, which is not wanted — each push should ring
  separately. Tested as a detector signal instead and it carries nothing: block-mate
  corroboration predicts holding at 71.2 / 70.2 / 73.1 / 71.2% (flat), abnormal co-firing
  contradicted the block model three times in twenty-three years, and it does not flag a
  bad print (4.3% against a 4.2% base rate).
- **`/floor` and the feedback recorder** — deleted. The command never changed a number in
  the project's life and the recorder collected one verdict, while between them they cost
  two of the six steps in the hourly pass, which is why that part of the order was
  load-bearing. The pass is four commands now. The `min_move_sigma` gate stays and is
  edited by hand (now at two usual hours, see "One size floor"); deleting a working gate
  because its setter was unused would be the wrong trade. What is genuinely given up is that
  nothing records whether a message was worth reading.
- **`schema/event_export.schema.json`** — deleted, with its `jsonschema` dependency.
  Nothing produced the export and no test validated it, despite a comment saying one did.
- **299 citations of the deleted specification** — removed across 46 files.
- **Run health measured by a throwaway script** — replaced by `tools/run_health.py`, which
  counts hourly slots with no successful run rather than failed runs, because an outage
  produces none of the latter.
- **What the shallowest rung is for** — restated. It was described as "near ten messages
  per instrument-year", which is how it was seeded and not something a reader can see.
  What it decides is the length of the weekly note: 7.3 digest rows a week across two
  notes, and three quarters of them are `noticeable`, so the first column sets the note's
  length almost alone and the three above it only decide which row carries which word.
- **Re-seeding the rungs after the sigma estimator changed** — checked, nothing to do. The
  ladder was seeded against one measurable criterion, near ten messages per
  instrument-year at the shallowest rung, and under the EWMA estimator the blocks run 8.1
  to 12.2. Spread across the basket is 3.8x, recall on large-and-unexplained episodes
  90.7%, and the detector fires harder in a crisis rather than quieter (4.9x in Oct 2008,
  9.7x in Mar 2020), which was the one failure a faster-adapting sigma could have caused.
  What is left is the preference about what each word means, and a preference does not go
  stale when an estimator changes.
- **Spacing the rungs the way a magnitude scale is spaced** — recorded, not adopted. Bottom
  rate, top rate and step size fix each other: pick two and the third follows. The bottom
  and top fire 288 and 15.3 times a year basket-wide, a ratio of 18.9x, which is 1.28
  magnitude units — so at the seismologist's one-unit step (10x rarer per class) there is
  room for **two** categories, not four. Four names at a 10x step would put `extreme` at
  0.29/yr across all 61 instruments, one every three and a half years. The ladder keeps
  four names by stepping 0.43 units instead. Changing that is a question about what the
  words should mean, and it has not been asked yet.
- **Routing on anything but the tier** — measured on the whole archive, then refused. The
  claim was that events found by both ladders at once hold up better and could be routed
  on for free. The effect is real (79.5% still standing against 71.0% and 69.3%, and it
  survives conditioning on tier), and the standardised residual `|z_resid|` is a better
  ranker still — at today's 43 pushes a year it holds 81.2% against the tier rule's
  75.6%, in every era. It was refused because of what pays for it. Re-cutting the rungs
  on `|z_resid|` drops the median record a push announces from 1.03 years to 0.27, and
  that sentence — *the biggest move since 3 March 2020* — is the product. Keeping the
  rungs and gating `major` on the residual holds the record at 1.06 years and 79.4%, but
  costs a third of the messages. Nothing recovers both.

  Two findings from that work are worth more than the verdict. **Retention is flat across
  the three lower rungs** — 70.3%, 73.0%, 71.6%, then 83.8% at `extreme` — so the ladder
  separates on size, as designed, and size barely predicts holding until the top rung.
  And **raw size in sigma predicts holding no better than chance** (71.8% against a 71.4%
  base rate) while the residual predicts it well. Big moves do not hold; unexplained ones
  do.
