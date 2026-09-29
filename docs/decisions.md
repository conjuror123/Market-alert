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

**One event per 24 hours of real time.** The reader's rule. A storm of hours is one story,
so an instrument's moves inside the 24 hours from its first one found are one event, shown
by its biggest hour and worded by its rarest. Real hours, not a trading day or a count of
candles: the same span for a coin, a pair and a fund, whose afternoon move and next
morning's open then read as one story. It replaced "one event a day unless the day grows",
which split a storm at midnight New York and sent a second message for each rise. Over the
record 28,095 flags become 19,185 events.

**`high` and up push; `noticeable` goes into the note.** The reader's choice, to try: more
messages than the previous detector's one a week was the point of the change. For today's
basket that is about 9 pushes a week and about 16 note rows a week, each row with its own
small ping; the busiest week of the last year had 32 pushes and 52 rows. `noticeable`
alone is two thirds of the events, so it is the one word worth reading in a batch; `high` (5.5σ) is where a move stops being routine for its instrument.
To be tuned with the threshold at stage 12, against a few weeks of real messages.

**One note a week.** With every word from `high` up pushed, the note holds only
`noticeable` rows, and one note a week holds them (it used to be two, Monday and Saturday).
The economic calendar stays a message of its own, sent in the same run just before the note
opens: one is a forecast, the other a report, and they are read differently.

**The week turns at the run after its last funds close.** The reader's choice, with stage 4.
Every move is checked at the funds' close after it, so a week that turns just after its last
close holds every one of its checks: the run that turns it finishes the old week first, and
the closing hour — found in that run — belongs to the new week and is checked on Monday. A
boundary before the close would strand Friday's checks in a closed week; one at Sunday 00:05
UTC left every weekend coin move waiting for Monday in the next. It follows New York, not
UTC — the one exception to UTC everywhere, and invariant 3 already allows it where the funds'
session is the day.

**Held at the close: one check, at the funds' close, for everything.** The reader's rules,
stage 4. Measured over the record, a check far out mostly measures the market's wandering
after the move: by the next close a random walk alone would already say "gave back" for
14–26% of `high` moves, and a Friday move's next close lands in the next week. The
same-session close is where the real fade shows clearly above the noise (a fund's `high`
hour: 79% still half there against 95% for a random walk). Coins and pairs take the funds'
close too rather than a span of their own, so that every check of a week lands in it. It is
said as a percentage only — 100% held exactly, 120% kept going, -20% reversed — on the time
line, not on lines of its own, and counts down in hours until then ("close in 5h", "next
close in 72h" when the close is on a later day). Filling it in is not a change to the event:
it adds no story.

**A jump message claims only what the detector measured.** The square's colour is the word,
so the word is not written out; the size is `|move| / σ` to one decimal, `11.0×σ`, at the end
of the first line, in the ping as in the push. Hour or gap is not spelled out as a yardstick
("its usual weekend gap"); the rarest-since line says which it was. Dates read `24.09.2026`.
No block/own split until its stage exists — the previous detector's wording for it would
describe a measurement the jump detector does not make.

**Rarest since: the last move at least this rare, of the same kind, the same way.** The
reader's choices, stage 3. *In σ*, the number the colour comes from, so the line and the
square speak one ladder. *Of the same kind*: a night's and a weekend's σ are not an hour's,
so only within a kind do two sizes in σ describe comparable moves — which is why the line
names the kind. *The same direction*: "rarest drop" is what a reader checks against a chart.
*At least 95% of the size, or bigger*: an exact record passes over a 4.9σ half a year ago to
name a 5.0σ two years ago, and the 4.9σ is the truer answer to "when did it last do this";
a bigger move always counts, so a 5σ with none like it but a 7σ last year says "in 1 year".
*Elapsed, rounded down*, so the claim stays true. *Always shown*, even "in 2 days": half of
`noticeable` hours say less than a fortnight, and that is itself the information. It
replaces the previous detector's "biggest move since N day ago", which compared gaps with
gaps of any kind and matched only an exact record.

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

**The week is curated; an earlier week is history.** The reader's rule. For the week of the
open note, the channel says what the detector says now: a move that is gone is deleted, a
move whose numbers or word changed is edited where it stands, a row that turns out to be a
push becomes one. Once the next note opens, the old week is a record and nothing in it
moves — before this rule one cold rebuild grew a note that had closed two days earlier from
5 rows to 19 and posted the difference as two alerts at breakfast. The channel is public and
the bot an admin, so Telegram lets it edit or delete at any age; the limit is ours.

**An event rings only inside its 24 hours; rarer rings again, milder never does.** The
reader's rules. Inside the 24 hours an event that turns rarer — a bigger hour, a fix, a late
bar — is deleted and sent again at its new word, so the phone says what the move now is and
the channel keeps one message for it; a ⬜ push that turns `high` again rings again too.
Milder is an edit: the reader already heard about it, and a falling word is not news worth a
buzz. After the 24 hours the event is complete and a fix can only correct it silently — a
row that turns `high` leaves the note and its own ping becomes the push, by an edit — and an
event corrected away then stays gone. A detector update is not a change to an event: it
restarts the week.

**A changed event tells its story.** The reader's rule: a message that was edited, or sent
again, says on one line what it went through and why — `✏️ ⬜ 4.0×σ 10:00 → 🟧 7.9×σ 12:00
bigger jump` — so an edit is never silent about being one. A clean event says nothing. The
reasons are the jump detector's own: the only things that move |move| / σ of an event's
biggest hour are a bigger hour, a late bar, a revised price, a revised yardstick, or the
jump disappearing. The story sits on the push and on the note row, not on the ping.

**A move belongs to the note open when it is found, and it is found only once it can be
judged.** The run fires at :05 and stores the hour it stands in, five minutes of it, so
scoring that bar judged a move on a few per cent of its volume. An hour is judged once it
has ended; a fund's gap with its first half-hour, once it has ended; a currency pair's
weekend gap at its open, which is the whole of it.

**A detector update restarts the week under the same note.** A new detector's events are
not the old one's, so correcting the old messages against them would be rewriting them with
another instrument's readings. The reader's rule: delete the week's pushes and pings, keep
the note and the calendar, and go on from that run as if on a new channel — nothing found
before the update rings. The update is a hash of the parsed code and the basket, so a
comment does not count.

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
