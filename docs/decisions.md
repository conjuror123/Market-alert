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
basket of 173 that is about 22 pushes a week and about 49 note rows a week, each row with its
own small ping, over the year to 2026-10-01; the busiest week had 87 pushes and 142 rows. `noticeable`
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
until it finds none.

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
month at 0.989 or better (`tools/bitstamp_fill.py`).

**Futures are read one contract at a time.** Live bars come from the front contract;
Yahoo's continuous series mixes in other contracts' prints — 64 of coffee's hours since
2024-05 jump past 3% and straight back, against 2 on a single contract — so it is used only
for history, cleaned once (`tools/futures_history.py`). Each series rolls on its liquid
months before first notice, and the night across a roll, which is the spread between two
contracts, is not scored. A bar on under 5% of the series' usual volume is a quote, not a
trade, and is a hole: a third of the moves past 6σ sat on such bars, each undone the next
hour (`tremor/futures.py`). Cotton's continuous history holds a third of a normal month in
14 of its 29 months and is not used; its record starts with its contracts' own bars.

**Nickel's quote glitches are dropped where it is fetched.** Kitco's gateway jumped 18–86%
in one five-minute step and came back five times in its history; no real step that size
has come back (2022-03-21, −22%, stayed). A step past 15% that returns more than halfway
within 24 hours is dropped; one not yet decided is held back, so a real 15% five-minute move
would reach the store a day late (`price_monitor/kitco.py`).

**Tin is Shanghai's.** The Shanghai Futures Exchange's main contract, hourly from 2019-08
(`tools/sina_history.py`), in yuan with VAT, labelled Tin (Shanghai). LME tin is served by
the hour only by Sina's chart endpoint, and only its last 1,023 bars — from 2026-07. Over
their shared clock hours the two correlate 0.85 hour by hour and 0.91 day by day.

**A candidate source answers four questions, cheapest disqualifier first:** does it serve
the bar that closed at :00 by :05; how many requests an hour does it allow against what it
would carry; does it agree with the consolidated tape on the thin names
(`tools/fund_verdict.py`); which tickers does it hold, and how far back.

---

## The shape of the repository

**Derived data is not tracked.** The metrics and the event table are rewritten every run
and rebuild from the bars in about fifteen seconds.

**Bars commit once a week, on Saturday.** Git cannot delta parquet, so a commit stores every
byte of each shard it touches and the frequency is the whole cost. Nothing is lost by
waiting while every provider reaches back further than a week: each run fetches from the
newest committed bar (`operations.md`).

**Two stores, not one.** Parquet for columnar history, JSON for state where a whole-file
rewrite is the point. Nothing here needs a server.

---

## Settled and closed

Raised, dealt with, and not to be raised again.
- **The 2020-02-10 wall** — filled. Twelve Data's intraday archive stops there; Alpaca's
  consolidated tape (2016 on) filled 61 funds, 407,153 hours, each gated on a three-month
  overlap (`--deepen-alpaca`, 2026-10-01). Every fund reaches 2016 or its launch.
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
  | Binance's API | HTTP 451 from US runners; its public archive is used as a referee only |
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
