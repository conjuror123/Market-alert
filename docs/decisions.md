# Decisions

Why the bot is built the way it is, so a settled choice is not reopened: each entry is the
choice, then its reason. How each part works is in `docs/manual.md`.

**Contents:** The detector · Delivery · Data and providers · The second source · The
repository · Rejected · Open questions

---

## The detector

**An established test, not a home-made one.** The intraday jump test of Lee & Mykland
(2008, *Review of Financial Studies* 21:6). Every setting then has a published or a
measured reason; a hand-made threshold table has only taste.

**Bipower σ, not a standard deviation.** It averages products of neighbouring moves (L&M
eq. 8), so a jump in the window never pairs with itself. With squares, one 0.05 jump among
0.001 moves inflates σ more than 1.5× (`tests/test_tremor_jumps.py`).

**Half a year of calendar time, for every instrument.** Volatility regimes follow the
world's calendar, not a market's hours. Half a year is 880 bars for a fund, 3,130 for a
pair, 4,380 for a coin, all inside the paper's valid range (√(252·n) to 252·n bars).

**A young series scores from the paper's minimum** (42 bars for a fund, 78 for a 24-hour
market), its rows marked `young`. A half-year warm-up would leave thirteen funds blind
through March 2020.

**Words at 6 / 8.5 / 12 / 17σ.** On this basket, doubling a move's size in σ makes it
about ten times rarer, so levels √2 apart make each word about three times rarer than the
one below. The 6σ bottom is a volume choice: moves of every size hold at the close alike
(76–80%), so a higher bottom buys fewer messages, not better ones. At 6σ: about 9 messages
a week; for one instrument a word about every 3 months, 7 months, 21 months, 4 years.

**Rarity is measured, not read off the bell curve.** Against its own half-year σ, an hour
of every class reaches 3.9σ about once in 115, not once in 10,000.

**Gaps are read against their own kind.** Nights against nights, weekends against
weekends, over the same half-year. Clock time does not scale a gap: a 65-hour weekend
moves only 1.17× a 17.5-hour night (French & Roll 1986). Borrowing σ from trading hours
predicts the next gap worst of all yardsticks measured. Separate kinds give one rule for
both calendars, since a pair has only weekends.

**A missing hour is skipped, not stretched.** The bar after a hole is its own hour; only
the jump across the hole is set aside. Measured across the hole, three quiet hours read as
one violent one. Only closures the calendar knows are gaps.

**Beyond 1,000σ is a broken print, and so is the stretch after it.** The line sits far
above the largest real readings (PFF −207σ, 2015-08-24; the franc −118σ, 2015-01-15).
Dropping one shrinks later yardsticks, so the check repeats until none is left. The hours
after a break go with it until the price is back within half of it, at most 24
(USD/KRW sat at 2.3 instead of 1,294 for three hours on 2024-01-01).

**One event per 24 real hours.** A storm of hours is one story. Real hours, not a trading
day, give every instrument the same span, and join a fund's afternoon move to the next
morning's open.

**Rarest since: the last move at least 95% as large, of the same kind and direction.** In
σ, so the line and the colour speak one ladder; same kind, because a night's σ is not an
hour's; same direction, because that is what a reader checks on a chart; 95%, so a 4.9σ
half a year ago answers for a 5.0σ today. Elapsed time is rounded down so the claim stays
true, and always shown, because "in 2 days" is information too.

**Held at the close: one check, at the first NYSE close after the move, for everything.**
That close shows a real fade above the noise (a fund's `high` hour: 79% half held, against
95% for a random walk); further out, a random walk alone says "gave back" for 14–26% of
moves. Coins and pairs use it too, so every check of a week lands in that week.

**A detector update restarts the week.** A new detector's events are not the old one's, so
the old messages are not corrected against them. The version hashes parsed code, so a
comment does not count.

---

## Delivery

**Rarity is the instrument's; interruption is the reader's.** A word depends only on its
instrument, so adding instruments changes no word's meaning. How often the reader is
interrupted is set by which words push.

**No weekly cap.** A detector that falls silent after its third alert fails in exactly the
week records cluster. Volume is set where it is generated: the bottom and the push words.

**`high` and up push; `noticeable` goes into the weekly note.** `noticeable` is two thirds
of events and worth reading in a batch; `high` is a move its instrument makes about twice
a year. About 4 pushes and 11 note rows a week.

**The week turns at the run after its last NYSE close.** Every move is checked at the next
NYSE close, so a week ending just after it holds all its checks. The economic calendar is
its own message, sent just before the note: a forecast and a report are read differently.

**The open week is curated; earlier weeks are history.** For the open week the channel
says what the detector says now. Once the next note opens, nothing of the old week moves,
so a cold rebuild cannot rewrite a closed record or post its difference as new alerts.

**Rarer rings again inside the 24 hours; milder never rings.** A rarer event is news; a
milder one the reader has already heard. After the 24 hours a fix corrects silently, and an
event corrected away stays gone.

**A changed event says so on one line**, so an edit is never silent:
`✏️ ⬜ 6.2×σ 10:00 → 🟧 12.4×σ 12:00 bigger jump`.

**The moves one run finds share messages, biggest first, and only the first rings.** News
moves dozens of instruments at once (130 events in the FOMC hour of 2024-12-18), and
Telegram takes about twenty messages a minute into a channel. An alarm leads with what
matters most. After the first message the reader is on the channel already. Grouping by
block folds floods worse.

**Size in σ, word as the colour.** A percentage means nothing across a basket where short
Treasuries move 0.024% in a usual hour and a coin several per cent.

**A reading is judged only once its bar has ended.** The run at :05 holds five minutes of
the current hour.

**Quality is recall on the obvious, not precision.** Of each instrument's top 0.01% of
hours, how many are flagged and at which word (`tools/stage_report.py`). Precision
rewards delay.

---

## Data and providers

**One provider per instrument, chosen by measurement**, against the stored bars hour by
hour, in basis points, beside an hourly σ of 20–40 bp. A feed a few bp off on a thin fund
sends alerts for moves that did not happen.

**A candidate source answers four questions, cheapest first:** does it serve the :00 bar by
:05; does its allowance cover the load; does it match the consolidated tape on thin names
(`tools/fund_verdict.py`); which tickers, how far back.

**A fund goes to an IEX-only feed only if IEX prices it like the tape** (median ≤ 2 bp,
p90 ≤ 5, ≤ 2% of hours missing, over 28 days). IEX is one exchange; thin commodity funds
miss by far.

**Documented APIs first, undocumented endpoints last, no quota past about 85%**, leaving
room for tests and backfills. Sina and Yahoo alternate within each block, so an outage of
either leaves every block reporting. TUR is on Google Finance, the only free feed matching
the tape on it.

**The store is unadjusted; corporate actions are declared.** Adjusted series change
retroactively with every dividend. Dividends and splits come from Tiingo's daily endpoint,
since a ratio of adjusted to unadjusted cannot see a split.

**History from another source is written only where it agrees with the store**: returns
correlate ≥ 0.90 and the median level gap is ≤ 25 bp over ≥ 200 shared hours
(`backfill.verify_alignment`). The level test catches dividend-adjusted imports, which
correlate at 0.996 while sitting 1% off. Only missing hours are written.

**Futures are read one contract at a time.** Yahoo's continuous series interleaves months.
The softs' history is Dukascopy's single-contract CFDs from 2018, spliced at a measured
seam; live cattle keeps Yahoo's series minus its interleaved stretches. The night across a
roll is not scored, and a bar under 5% of usual volume is a quote, so a hole.

**The LME's metals are the LME's own, from Sina**: the right instrument with a short record
(from 2026-07) over a longer record of another (Shanghai tin, Kitco, COMEX aluminium).

**Every coin is its USDT pair on Binance**: one exchange, one quote currency. Below a 2018
seam, before USDT left the dollar, the record is the deepest dollar exchange passing the
splice gates, scaled to meet without a step.

**A provider that does not answer twice in a row is stopped for the run.** An unanswered
request costs about 96 s of timeouts and retries (186 s at Alpaca), and the job has 20
minutes. Asked one by one, a silent Yahoo's 38 funds and 133 dividend lookups come to 274
minutes on a simulated clock (2026-10-05); stopped after two, to 3. A cancelled job
delivers nothing and tells no one. One silence can be a blip; two in a row is the provider.
A wrong stop costs its instruments an hour, and the health chat names it. A refusal (404,
400) is an answer and stops nothing.

**USD/BRL trades in its own session**, 09:00–18:00 São Paulo (`b3_fx`): outside it real
moves are under 3.5 bp an hour.

---

## The second source

**A move another feed did not see is not scored, and a sent message is marked, not
deleted.** A real trade shows on another feed; a bad print does not. Kept out of the
yardstick, a bad print cannot inflate the next half-year's σ. Marked rather than deleted,
a wrong verdict costs a line, not a real move.

**The verdict answers one question: did the other feed move with it?** An hour of lag is
allowed, a missing hour bridged. Which feed was wrong is not asked: second sources lag, miss
hours (no USD/KRW bar on Seoul's martial-law night, a real +2.5%) and print their own bad
ticks, and every culprit rule breaks a real case.

**Bars around the move outweigh a bridge across it.** A bridge only says the price got
there somehow.

**Candidates are the detector's own readings at ≥ 4σ.** A check with its own σ missed 23
of 543 flagged readings after a calm stretch.

| instruments | verified against | why |
|---|---|---|
| FX pairs | Yahoo and MarketWatch | MarketWatch has every hour (Yahoo 21–72%), 0–0.6 bp off |
| funds fed by Yahoo | Sina 30-minute | matches the tape to 0.0 bp |
| other funds | Yahoo 30-minute | independent of the funds' own feeds |
| coffee, cocoa, cotton | Sina global futures | 0.2–2.2 bp off, hourly correlation 0.96–0.99 |
| live cattle | MarketWatch continuous | hourly correlation 0.92 |
| coins, LME metals | none | no independent free feed |

---

## The repository

**Derived data is not tracked**: it rebuilds from the bars in under a minute.

**The open month lives on a release; a month enters git once, settled.** Git stores
snapshots, so hourly commits into per-instrument files cost 124 MiB a year; replacing a
release file costs nothing. The Actions cache evicts after seven idle days and bars cannot
be refetched, so it holds only the metrics.

**A settled month is plain CSV; a finished year is Parquet.** A month waits a week after it
ends so outage holes get filled. Git compresses everything, so gzip gains nothing, and a
month is too small for Parquet; a year is not. About 28 MB of git a year.

**Health reaches the chat from wherever the run can still speak.** The streak lives in the
monitor, so what happens before it - a setup step, the open months' restore - or after it -
the commit - is said by the workflow's last step with curl, and hours with no run by the
next run. Both time an outage by one clock, the last run whose state was committed
(`last_run_utc`), not by counting runs: a count read off the run history is capped by its
page and stops moving. A step that does not stop the run (the session table, the open months' save) is a
failed run in the streak: broken, if not yet urgent. Per-instrument problems (a provider
down, a stale instrument) go out every run or daily, never through the streak, because the
streak is about the run and one instrument rarely fails three hours alike.

---

## Rejected

Measured or weighed, and not to be raised again.

| option | why not |
|---|---|
| time-of-day factor (Boudt, Croux & Laurent 2011) | flags it removes reverse no more often than the rest; flags it adds reverse more |
| labelling scheduled news | release-hour jumps are real; a push already lists nearby releases |
| block co-jumps (Bollerslev, Law & Tauchen 2008), block-residual move, size floor | blocks are tight, so a common move shows in its members; shared messages fold floods better |
| a Student-t tail per class | every class has nearly the same tail against its own σ |
| per-instrument "N times a year" levels | sends the biggest crisis hours out as note rows |
| σ·√k across a hole | needs a clock of expected hours and messages saying "over 3 hours" |
| a level check (30% off the two-day median) | drops real crypto crashes |
| "isolated and taken back" as a bad print | marks real moves, misses a feed sitting off for hours |
| holding isolated moves an hour | a third of alerts would be an hour late |
| a watchdog for the trigger's silence | the external scheduler is the party that knows a call stopped |
| Finnhub, Metal Sentinel, FXEmpire, Business Insider | quotes, not bars |
| Financial Modeling Prep, Massive (Polygon) free tiers | daily bars, or today's bars only after the close |
| Eulerpool, London Strategic Edge | no hourly bars for the funds needed |
| Kitco, Shanghai tin, COMEX aluminium, WisdomTree metal ETCs | not the LME's market (hourly correlation ≤ 0.85, or thin) |
| TradingView | ~6,300 bars without a login; its terms |
| Investing.com, the LME, CNBC, Barchart, Boursorama, CME | refuse or forbid automated readers; not bypassed |
| DailyFX, Stooq | gone; login required |
| Sina forex | six months of hourly bars |
| Sina live cattle | quotes without volume, correlation 0.80 |
| FT's chart endpoint | five days of hourly bars; not needed beside MarketWatch |
| api.binance.com | HTTP 451 from US runners; the market-data mirror is used |
| FXCM history | spliced blind; Dukascopy gates on a measured overlap |
| Interactive Brokers, paid Barchart | a funded account or about $500 a month |

---

## Open questions

Known and left alone on purpose.

**The fund feed verdict rests on one calm month.** Feeds that agree when quiet can part in
a violent month. To act: rerun `tools/fund_verdict.py` and `tools/sina_probe.py` right after
the next one, while the feeds still serve it (Yahoo ~55 days, Sina ~78).

**Weekend yardsticks are the noisiest** (±16%, against ±3% for hours). Pooling a fund's
weekends with its nights predicts a little better (QLIKE 1.94 against 2.10), a 2-year window
better still for pairs (2.96 against 3.90). To act: a one-line change in `tremor/jumps.py`,
then `tools/stage_report.py`.

**Repository size**, about 640 MiB packed, mostly superseded Parquet in history; GitHub
warns at 1 GB. To act: a history rewrite, which force-pushes the live branch irreversibly.

**Undocumented endpoints carry most of the basket** (Yahoo, Sina, Google Finance,
MarketWatch). A changed shape raises and the health chat names the provider. To act: paid
feeds.

**History no free source reaches.**

| instruments | missing before | tried |
|---|---|---|
| 55 US funds | 2016-01 | HF Data, Twelve Data, Alpaca's tape |
| live cattle | 2024-05 | Yahoo (730 days), Dukascopy |
| coffee, cocoa, cotton | 2018-01 | Dukascopy |
| LME tin, nickel, aluminium | 2026-07 | Sina (last 1,023 bars) |
| USD/BRL, USD/INR, USD/KRW | 2019-09 to 2020-01 | Twelve Data, Dukascopy, Sina forex |
