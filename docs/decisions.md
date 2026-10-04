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
Studies* 21:6). It is built in stages, each with its source and a measured before/after, so
what a message means can be followed. Two refinements from the same literature were
measured and dropped: the time-of-day correction of Boudt, Croux & Laurent (2011) and the
group ("co-jump") test of Bollerslev, Law & Tauchen (2008) ("Settled and closed").

**Stage 0 is two rules and nothing else.** The hour's move over the instrument's bipower
volatility, and a word from the size of that ratio. One-a-day, channels, the date, the held
check and the gap came back as their own stages.

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

**The words are about √2 apart, half an earthquake magnitude, rounded to 6 / 8.5 / 12 / 17.**
Rounded so the levels are the numbers the docs and the reader use (an exact √2 put them at
8.485 and 16.97; rounding moved 32 readings in all history from high to noticeable and 5
from extreme to major). Measured on the basket, doubling
a move's size (in half-year sigmas) makes it about ten times rarer — a Gutenberg–Richter
law with a slope near one — so a √2 step makes each word about three times rarer than the
one below: over five years the levels that make each word exactly three times rarer from
6σ are 6 / 8.4 / 11.7 / 16.8. A step of 1.5 would make it about four times, 2 about ten
times (whole magnitudes) — and with 2, pushes all but vanish (1.7 a week).

**The bottom is 6σ (stage 12), the owner's choice from measured volumes.** The bell curve's
"3.9σ is one hour in 10,000" does not hold: against its own half-year σ an hour of every
class reaches 3.9σ about one time in 115 (stage 10, below). Small and big moves hold at the
close alike — 76-78% of moves of 3.9-4.5σ still had half their move at the next close,
against 76-80% at every larger size — so a higher bottom buys fewer messages, not better
ones, and it is set by how much the reader wants to hear. Delivery replayed hour by hour
over the year to 2026-10-01: 29.1 messages a week at 3.9, 21.1 at 4.5 (bottom only), 15.1 at
5 / 7.1 / 10 / 14.1, 9.0 at 6 / 8.5 / 12 / 17 (7.9 ringing). Over all the history,
2004-2026, with the instruments that did not yet exist filled in from their own rate times
their class's turbulence that year: about 10 messages a week, from 4.4 (2009, judged against
the half-year after the 2008 crash) to 17.9 (2008); the busiest week, 9-15 March 2020,
about 107. For one instrument the words come about every 3 months (noticeable or rarer), 7
months, 21 months and 4 years.

**What the papers say about using what we know of an asset.** Model known repeating
patterns — the time of day (Boudt, Croux & Laurent find it recovers small jumps in quiet
hours and removes false ones in busy hours). Do not filter news: about a third of FX jumps
coincide with US data releases (Lahaye, Laurent & Neely), and they are the real thing.
Fat tails are what a jump test detects, so a per-asset tail shape is a risk-management
idea. All three — time of day, news labels and the tail shape — were tried or weighed here
and dropped (below, "Settled and closed").

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
record 16,897 flags become 12,662 events.

**`high` and up push; `noticeable` goes into the note.** The reader's choice, to try: more
messages than the previous detector's one a week was the point of the change. For today's
basket of 173 that is about 4 pushes a week and about 11 note rows a week, each row with its
own ping line, over the year to 2026-10-01; the busiest week of that year had 22 pushes and
43 rows. `noticeable` alone is about two thirds of the events, so it is the one word worth
reading in a batch; `high` (8.5σ) is a move its instrument makes about twice a year.

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

**One message a run, biggest first.** Big news moves dozens of instruments in one hour: the
FOMC hour of 2024-12-18 found 130 events, 108 of them pushes, and Telegram takes about
twenty messages a minute into a channel, so one message per move arrived over hours, late
and out of order. The moves one run finds share messages instead — its pushes in one, its
pings in one more — ordered by size in σ, largest first: a message is an alarm, and leads
with what matters most, where the note is a record and runs by time (by size only inside
an hour). Measured at the 3.9σ bottom of the time, delivery replayed hour by hour over the
year to 2026-10-01: 29.1 messages a week (9.2 of pushes, 16.6 of pings, 3.3 note parts) for
69 events a week, and the FOMC hour of 2024-12-18 in 6 alert messages instead of 130. Each move keeps its own life inside its message; turning rarer inside
its 24 hours takes it out and rings it again in the run that finds that. **Only the run's
first message rings**: after it the reader is on the channel, so the pings after a push, a
flood's further messages and a part the note grows by are silent — 21.8 rings a week then,
7.9 at today's 6σ.
Grouping by block (stage 7) was the other way to fold a flood, and was measured worse: by
the same estimate from the events table, 25.7 messages a week against 22.9 for one message
a run, and 9 in the worst hour against 6.

**A move belongs to the note open when it is found, and it is found only once it can be
judged.** The run fires at :05 and stores the hour it stands in, five minutes of it, so
scoring that bar judged a move on a few per cent of its volume. An hour is judged once it
has ended; a fund's gap with its first half-hour, once it has ended; a currency pair's
weekend gap at its open, which is the whole of it.

**A detector update restarts the week under the same note.** A new detector's events are
not the old one's, so correcting the old messages against them would be rewriting them with
another instrument's readings. The reader's rule: delete the week's pushes and pings, keep
the note and the calendar, and go on from that run as if on a new channel — nothing found
before the update rings. The update is a hash of the parsed code (sessions and rolls
included), the roll table and the basket, so a comment does not count.

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

**A missing hour is skipped, not stretched.** Measured from the last close before it, the
bar after a hole read three quiet hours as one violent one: 122 of 28,940 flagged readings
were such moves, CPER's and USD/CNH's among them at `extreme`. Two fixes were weighed and
dropped: judging a k-hour move against σ·√k, which needs a clock of expected hours per
instrument and a message that says "over 3 hours"; and not scoring the bar after the hole,
which throws away an hour that was really measured. The bar after it is its own hour, open
to close, like a session's first bar; only the jump across the hole is set aside. A missing
first hour of a fund's day still leaves the night scored, to the first bar there is: one
quiet hour is small next to a night, and dropping the night lost 1,287 of them. Only closures the calendar knows are gaps: a fund's nights and weekends, a
pair's weekends and its Christmas and New Year closures (2026-09-30), the latter judged with
its weekends because a pair has no nights to compare them with. In the last year this
skips 68 holes, 150 hours: UGA 39, two Coinbase outages of five hours on all nine coins,
and five single hours at the Sunday open of GBP/USD and NZD/USD.

**A reading beyond 1,000σ is a mistake, and is dropped.** Coinbase reopened after an
outage on 2017-04-15 on a bitcoin print of $0.06, which read as +1,526σ and swelled BTC's
yardstick for half a year, hiding 24 of its hours. The line sits well above the largest
real readings in the history - PFF's open on 2015-08-24 (-207σ as a weekend, +147σ as an
hour) and the Swiss franc's unpegging on 2015-01-15 (-118σ) - which a 100σ line would have
thrown away. Dropping one can only shrink the yardsticks after it, so the check repeats
until it finds none. A break can last: USD/KRW sat at 2.3 instead of 1,294 for three
hours on 2024-01-01, and the hour inside read 32.8σ against a yardstick the stretch had not
reached - so the hours after a break go with it until the price is back within half of it,
at most 24 (`jumps.STRETCH_BARS`). In all history that removes that one reading. A level
check (30% off the two days' median) was weighed and dropped: it also drops real crypto
crashes (2020-03-13, 2021-05-19).

**A move another feed did not see is not scored - and is marked, not deleted.** A real
trade shows up on another feed; a source's bad print does not (`tremor/verify.py`). The
shape was tried first and dropped: "an isolated move taken straight back" marks 157 bars in
the record, but USD/TRY's -0.42% on 2025-03-14 20:00 was taken back within the hour and
Yahoo shows -0.58% - real - while SiftingIO's USD/INR sitting 0.3% over the market for two
hours passes it. Holding every isolated move until the next bar was dropped too: a third of
events are isolated, so a third of alerts would arrive an hour late.

Deciding *which* feed was wrong was tried and dropped as well (2026-10-04). Each fix for
one case broke another: the second source lags by an hour (USD/TRY came back at 11:00 on
SiftingIO, 12:00 on Yahoo), misses hours (no USD/KRW bar between 07:00 and 15:00 on the
night of Seoul's martial law, 2024-12-03 - a +2.5% that a stricter rule called a mistake),
and prints its own bad ticks (Yahoo's USD/TRY dipped 0.4% for two hours on 2026-09-17). So
the verdict stays the one question two feeds can answer - did the other feed move with
it? - with an hour of lag allowed and a missing hour bridged by the bars either side. An
unconfirmed move is left out of scoring and out of the yardstick, so a bad print cannot
inflate the next half-year's σ; a message already sent says `⚠️ unconfirmed` rather than
disappearing, because a wrong verdict should cost a line, not a real move. Only feeds with
a free independent second source are asked: a coin's price is its exchange's own trades.
Coffee, cocoa and cotton are asked of Sina's global futures (2026-10-04): against the stored
bars from 2026-05, 0.2-2.2 bp apart at the median, hourly moves correlated 0.96-0.99, no
hour missing, and all 19 far moves since confirmed. Its live cattle is quotes without volume
correlated 0.80 hour by hour, and not used; no second free hourly feed of the LME's metals
was found.
MarketWatch (its chart endpoint, `price_monitor/marketwatch.py`) is asked beside Yahoo
for the pairs, and alone for live cattle (2026-10-04). Over its ten days it had every
stored hour of the 17 pairs (Yahoo's hourly FX has 21% of USD/INR's, 60-72% of the
others'), 0-0.6 bp from them at the median, and not SiftingIO's jump-and-back on USD/TRY
2026-09-24 19:00 - its quotes are its own. Its live cattle (continuous contract) moves with
the stored front month at 0.92 hour by hour. Its coffee is Yahoo's to the basis point, one
upstream, so the softs stay on Sina. Replayed over the ten days: 27 readings, 26
confirmed with both sources against 24 with Yahoo alone; the one Yahoo alone left
unconfirmed (USD/TRY +0.09%, 2026-09-24 19:00) MarketWatch shows an hour later, inside
the lag allowed. FT's chart endpoint also serves hourly USD/INR (five days), untried as a
source; CNBC, Barchart and Boursorama refuse readers, CME forbids them, and the WisdomTree
metal ETCs correlate with the LME at -0.25 to 0.54 hour by hour.
Over the 699 days Yahoo reaches (2026-10-04) it asked about 2,034 readings - every one of
the 543 the detector flags there among them - and did not see 115; 60 of those were
flagged - 49 of them SiftingIO's USD/INR, mostly between 22:00 and 01:00 UTC, and six
USD/BRL session openings whose first print was a stale pre-market quote - and with them
out of the yardstick 19 real moves of the same pairs now read as flags. The candidates are
the detector's own readings, built and scored by its own code: a check's own sigma of the
last few months missed 23 of the 543 after a calm stretch, and gaps against the hourly
sigma put 84 funds into one Monday-open run.
The martial-law hours stay (`major`, `high`). The closest call
left out: USD/TRY's +1.46% Sunday reopen on 2025-03-23, where Yahoo shows +0.61%.

**A day is not a unit of completeness.** Gap detection asks about hours: a day present with
three of its seven hours is a hole a day-level check cannot see.

**One provider per instrument, chosen by measurement.** Each candidate was compared against
the stored bars hour by hour in basis points, against one sigma of an hourly move (20-40
bps). A feed disagreeing by a few basis points on a thin fund is not a cheaper feed — it is
a source of alerts for moves that did not happen.

**A fund goes to an IEX feed only if IEX prices it like the tape**: median ≤ 2 bp, p90 ≤ 5,
and at most 2% of the tape's hours missing, against Alpaca's consolidated tape (SIP) over
28 days (`tools/fund_verdict.py`, 2026-09-30). IEX is one exchange; it passed 30 of the 89
candidates. The thin commodity funds fail it by far: UGA by 15.09 bp at the median and 60.17
at p90 (2026-09-22), one and a half to three sigma of an hourly move. The rest go to a
consolidated feed.

**Documented APIs first, Yahoo last, and no quota past about 85%**, so tests and backfills
have room. Tiingo holds 27 funds (its key is shared with production until the switch);
Alpaca's free IEX feed the other IEX-safe ones (fresh at :05, measured 2026-10-01); Twelve
Data 8, in one batched request a run on a thread beside the other providers (16 would add a
minute's wait to every run); SiftingIO the currency pairs (about 85% of its month). Sina
Finance and Yahoo split the remaining consolidated funds, alternately within each block, so
an outage of either leaves every block reporting: Sina's half-hour US bars matched the tape
at 0.0 bp (median and p90) on all 67 then on Yahoo, with 100% of its volume and no hour
missing (28 days to 2026-10-02, `tools/sina_probe.py`). TUR alone is on Google Finance's
quote page: every consolidated feed misses the tape on it by p90 5.6 bp, Google's page by
0.0.

**History from a second source is written only where it agrees with the store**: over the
hours both hold, returns correlate ≥ 0.90, the median level gap is ≤ 25 bp, on at least 200
hours (`tremor.backfill.verify_alignment`). The level test is the one that bites — a
dividend-adjusted import scored 0.9959 on returns while sitting 99 bp below the store. Only
hours the store lacks are written. A stored bar is replaced only from the consolidated tape
and by hand (`--repair-alpaca`; EZU's 2020-03-12 15:00 was +12.3% in the store, −1.2% on the
tape). A stretch with no overlap at all is refereed by a third market: Bitstamp's XRP over
Coinbase's 905-day suspension against Binance's archive, return correlation 0.9945, every
month at 0.989 or better (`tools/bitstamp_fill.py`; that record left with Coinbase).

**Futures are read one contract at a time.** Live bars come from the front contract.
Yahoo's continuous series mixes contract months - not only single hours (64 of coffee's
since 2024-05 jumped past 3% and straight back) but whole stretches of sessions that open
on one month and trade on the other: coffee 2026-07-22 to 08-10, cocoa 2026-03, cattle
2026-02-19 to 04-02, each a run of same-hour moves the size of the spread. So coffee's, cocoa's
and cotton's history is Dukascopy's CFDs (`tools/dukascopy_futures.py`), which hold one
contract at a time and switch once, from 2018-01: spliced to the listed contract at the
seam (coffee 0.9986 correlation, 1.7 bp; cocoa 0.9981, 2.9 bp; cotton 0.9971, 1.2 bp). The CFD switches 2 to 12
business days before this series rolls; where its switch stands out in its own data it is
listed in `data/tremor/rolls.csv` and its night unscored, and where none does the weeks
around the roll are dropped — in 2018 four of five rolls for coffee and for cocoa, so that
year has a four-week hole around most rolls. A session's last hour that closes far off and
is taken back by the next open (coffee 2018-05-21, -7.2% at 20σ; cocoa 2018-02-09) is a
print the CFD never traded at, and is set to the next open before the rolls are looked for
(`futures.reset_stray_closes`). Cattle, which Dukascopy does not carry, keeps Yahoo's series
with its interleaved stretches dropped (three sessions within fifteen that open past 1.5%
from the last close and come back) and stray opens judged on its own hourly moves
(`tremor/futures.py`). Each series' own rolls are made on its liquid months before first
notice, and the night across a roll is not scored. A bar on under 5% of the series' usual
volume is a quote, not a trade, and is a hole: a third of the moves past 6σ sat on such
bars, each undone the next hour.

**The LME's metals are the LME's, from Sina**: tin, nickel and aluminium as the three-month
contract's traded hourly bars, though Sina serves only the last 1,023 (from 2026-07). The
owner's call: the right instrument with a short record over a longer record of another
(2026-10-02). What they replaced, measured over their shared hours: Shanghai's tin, in
yuan with VAT, correlated 0.85 with the LME's hour by hour; Kitco's nickel quote stood
still in 20% of hours (Sina 2%), sat 98 bp below and correlated 0.61; COMEX aluminium
traded a median 5 contracts an hour against the LME's 901 lots.

**Every coin is its USDT pair on Binance, and a dollar exchange's record under its first
months.** The owner's call (2026-10-02): one exchange and one quote currency live, named
for what they are (ADA/USDT); and history combined where another public record reaches
further. Binance opened in 2017-08 thin: its hourly returns correlated 0.84 with Coinbase's
over its first quarter, 0.98 by mid-2018 and 0.99 from 2019 (BTC; ETH and LTC alike). So
below a seam - 2018-06-01, after Binance matured and before USDT slipped off the dollar
in late 2018, where seams fail the level gate - the record is the deepest dollar
exchange's that passes the splice gates over the two months after the seam, scaled by the
median ratio over the month after it so there is no step: BTC Bitstamp from 2013 (0.989,
8.7 bp), LTC Bitfinex 2013-12 (0.989, 7.1 bp), ETH Bitfinex 2016-04 (0.992, 6.0 bp), XRP
Bitstamp 2017-03 (0.974, 13.4 bp), BCH Coinbase 2018-01 with a 2019-01-15 seam (0.987,
24.3 bp). Each starts at its first month traded in 90% of hours. Pairs Binance renamed are
joined to their earlier name: POL to MATIC, BCH to BCHABC (+0.0% across each). LINK and
ADA traded against BTC on Binance before their USDT pairs opened, so below a seam where the
USDT pair's volume caught up their record is the BTC pair times BTCUSDT, hour by hour: LINK
from 2017-10 below 2019-05-01 (0.997, 7.7 bp after the seam), ADA from 2017-12 below
2018-06-01 (0.994, 5.8 bp). The other coins' BTC pairs open with their USDT pairs, or the
coin did not exist before.

**A candidate source answers four questions, cheapest disqualifier first:** does it serve
the bar that closed at :00 by :05; how many requests an hour does it allow against what it
would carry; does it agree with the consolidated tape on the thin names
(`tools/fund_verdict.py`); which tickers does it hold, and how far back.

---

## The shape of the repository

**Derived data is not tracked.** The metrics and the event table are rewritten every run
and rebuild from the bars in about fifteen seconds.

**The month being written is not in git; a month enters git once, settled.** Git does not
store changes, it stores snapshots: a commit that adds one line to forty files stores forty
whole new files (zlib-compressed, and only later, maybe, packed as deltas) plus the folder
listings pointing at them. Measured on the week of 2026-09-21, an hour's new bars are 650
bytes and the commit recording them in per-instrument files 14 KB: 124 MiB a year committed
hourly, 18 daily, 51 for weekly Parquet (which shares nothing with its previous version at
all). So the open months live as CSV on a GitHub release of the repository
(`tools/hot_bars.sh`) — replacing a release's file costs the repository nothing — and every
run downloads them, appends, uploads them back (`YYYY-MM.open.csv`, gitignored). The
Actions cache was the other place outside git, and was not chosen for bars: it drops an
entry nothing touched for seven days, and bars cannot be fetched again. The metrics, which
can, live there.

**A settled month is plain CSV, a finished year Parquet.** A month goes into git a week after
it ends, so a hole a provider's outage left in its last days has been filled (a later fill
rewrites that one month of that one instrument), and is otherwise never rewritten. Plain
CSV, not gzip: git compresses every file it stores, so August 2026 costs it 0.97 MB plain
and 0.98 gzipped. Not Parquet: 1.85 MB (1.3 tuned), because a month of one instrument is
too small for Parquet's per-file overhead to pay off, and no faster to read — 192 month
files load in 0.30 s as CSV, 0.39 as Parquet (measured 2026-10-02). A year is big enough
for Parquet to win, so once its December has settled the first write folds its months into
one `YYYY.parquet`. That costs git the year once more, about 16 MB (2025 was 16.4), but keeps
the store at a file a year for every load to open rather than twelve more each year. In
all about 28 MB of git a year. Years already in Parquet stay as they are: Parquet is the
format history is read in.

**Two stores, not one.** Parquet for columnar history, JSON for state where a whole-file
rewrite is the point. Nothing here needs a server.

---

## Settled and closed

Raised, dealt with, and not to be raised again.
- **The time-of-day factor (stage 5)** — built, measured and dropped (2026-10-02). Boudt,
  Croux & Laurent's factor, per instrument and hour of the day, over five years: hour
  flags -5%, events -7%. The busy hours' flags it removed reversed at the next close no
  more often than the rest (15-18%) — the open and the 08:30 and 10:00 New York releases
  are where the news is — and the quiet hours' flags it added reversed more (26%; FX at
  17-19 New York 36-49%); a pair whose home market is shut at night (USD/INR, KRW, TRY)
  got factors near 0.01 there. Raising the bar only in busy hours cut events 10% at the
  cost of real news. The owner's call: a busy hour fires more because more happens in it.
- **Labelling scheduled news (stage 9)** — dropped with stage 5, the owner's call
  (2026-10-02). A push already lists the releases in the hours around its move
  (`Nearby economic events`).
- **Block co-jumps and the own move (stages 7 and 8), and the size floor (11)** — measured
  and dropped (2026-10-03). Bollerslev, Law & Tauchen's test on a block's mean standardised
  move, against its own half-year, over five years: 4.6 block jumps a week at 3.9σ, of them
  0.2 a week — about ten a year — with no member already flagged. The blocks are tight, so a
  common move shows in the members themselves. What was left was folding floods, and one
  message a run does that better (Delivery, above). The own move (8) was the same block
  machinery, and the floor (11) existed only for it. The owner's call: anything more on
  blocks is overcomplication.
- **A tail shape per asset class (stage 10, Student-t)** — measured and dropped
  (2026-10-03). Against its own half-year σ every class has nearly the same tail: an hour
  reaches 3.9σ 71-110 times as often as the bell curve says (equity 71, FX 94, crypto 95,
  rates 98, precious metals 110), so 3.9σ is about one hour in 115 for all of them, not one
  in 10,000. A Student-t fits far better than the bell curve (ν 2.5-3.5 by block) and was
  run through the whole detector over five years two ways. Keeping the bell curve's rarity
  per word put `noticeable` at 14-24σ: 0.4 events a week, and 86% of each instrument's five
  biggest hours missed. Keeping the basket's rarity and adjusting only each class's shape
  moved events 62.8 -> 65.0 a week and messages about 3% down, shifting a few events a year
  from FX, agriculture and metals to equity; and the class thresholds this needs (3.6-3.9σ) differ by less than one
  class's own threshold moves between years fitted alone (up to ±0.7σ). Bajgrowicz,
  Scaillet & Treccani (2016) answer false detections with a higher threshold, not another
  distribution: that is stage 12.
- **The 2020-02-10 wall** — filled. Twelve Data's intraday archive stops there; Alpaca's
  consolidated tape (2016 on) filled 61 funds, 407,153 hours, each gated on a three-month
  overlap (`--deepen-alpaca`, 2026-10-01). Every fund reaches 2016 or its launch.
  IGIB and USIG, which looked launched in 2017-08, are funds of 2007 renamed in 2018: HF Data
  files their years before under CIU and CRED, and both passed the overlap gate from there
  (`backfill.FORMER_TICKERS`, 2026-10-02).
- **The feed split resting on one week (62 hours)** — superseded by the 28-day fund verdict
  against the consolidated tape (2026-09-30) and Alpaca's 30-day comparison (2026-09-22:
  SIP 44 of 44 at 0.00 bp, IEX 30 of 44).
- **A second consolidated live feed** — Sina Finance (2026-10-02), above.
- **Live IEX capacity past Tiingo's slots** — Alpaca's free IEX feed (2026-10-01).
- **USD/BRL's nights** — its own session, `b3_fx`, 09:00–18:00 São Paulo: outside it the
  real moves under 3.5 bp an hour, against 9–31 inside (2026-10-02).
- **Sources measured and not used**, so they are not measured again:

  | source | why not |
  |---|---|
  | Finnhub (free) | quotes only; a quote at :05 is not the :00 close |
  | Eulerpool | no hourly bars for funds |
  | London Strategic Edge | its catalogue lacks what was missing |
  | Financial Modeling Prep (free) | daily bars only; hourly answers 402 |
  | Massive (Polygon), free | today's bars only after the close |
  | Metal Sentinel (RapidAPI) | nickel and aluminium quotes, no history |
  | TradingView | about 6,300 hourly bars a symbol without a login, and its terms; used once, for research (USD/BRL's session) |
  | Investing.com, the LME, CNBC | refuse automated readers; not bypassed |
  | FXEmpire | quotes, and OANDA's CFDs |
  | DailyFX | gone |
  | Business Insider | tin once a day |
  | Kitco, aluminium | a second price 30% higher every few weeks |
  | Kitco, nickel; COMEX aluminium; Shanghai tin | replaced by the LME's own bars (above) |
  | api.binance.com | HTTP 451 from US runners; Binance's market-data mirror answers them and is used |
  | Coinbase, live | replaced by Binance for one exchange and one quote currency; its BCH record is used below the seam (above) |
  | Stooq | nothing without a login |
  | Sina forex | six months of hourly bars |
  | Barchart, Interactive Brokers | paid (about $500 a month) or a funded account; not tried |
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
