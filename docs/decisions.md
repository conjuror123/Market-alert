# Decisions

Why the bot is built the way it is, so a settled choice is not reopened: each entry is the
choice, then its reason. How each part works is in `docs/manual.md`.

**Contents:** The detector · Delivery · Data and providers · The sources' vote · The
repository · Rejected · Open questions

---

## The detector

**An established test, not a home-made one.** The intraday jump test of Lee & Mykland
(2008, *Review of Financial Studies* 21:6). Every setting then has a published or a
measured reason; a hand-made threshold table has only taste.

**Bipower σ, not a standard deviation.** It averages products of neighbouring moves (L&M
eq. 8), so a jump in the window never pairs with itself. With squares, one 0.05 jump among
0.001 moves inflates σ more than 1.5× (`tests/test_jump_jumps.py`).

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

**A detector update restarts the week, if it changes the week's events.** A new
detector's events are not the old one's, so the old messages are not corrected against
them. But the version hashes code, and a refactor or an error path moves it with every
event the same (2026-10-06: F8's isolation, 17,297 readings identical): then the week is
compared first and kept, so correct messages do not vanish over a change that moved
nothing. The version hashes parsed code, so a comment does not count.

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
event corrected away stays gone. A doubt lifted is not news: the move was
already told, so the mark comes off by an edit, and only a move rarer than the word shown
before the mark rings (decided 2026-10-06).

**A changed event says so on one line**, so an edit is never silent:
`✏️ ⬜ 6.2×σ 10:00 → 🟧 12.4×σ 12:00 bigger jump`.

**The moves one run finds share messages, biggest first, and only the first rings.** News
moves dozens of instruments at once (91 events in the FOMC hour of 2024-12-18), and
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
(measured once per feed against the tape); which tickers, how far back.

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

**Splits are declared from Yahoo, every morning and once for the history.** The ratio guard
knows only the common ratios, and the table held only the five SPDR splits of 2025-12-05.
Yahoo lists 56 fund splits since 2000; stocksplithistory.com matched every one. 53 had been
adjusted in the store. IGIB's and USIG's 2-for-1 were not, but the guard caught them.
USHY's 6-for-5 of 2019-04-22 was not, and read as an −18% night (−103σ, extreme). Declaring
them removed that one reading over the full history. Because the hourly run recomputes only
its last rows, a fund whose payouts or splits change is rebuilt, or the old day's gap would
keep the old answer.

**History from another source is written only where it agrees with the store**: returns
correlate ≥ 0.90 and the median level gap is ≤ 25 bp over ≥ 200 shared hours
(`backfill.verify_alignment`). The level test catches dividend-adjusted imports, which
correlate at 0.996 while sitting 1% off. Only missing hours are written.

**The majors' 2012 Sunday opens are Dukascopy's.** That year's source opened every bar at the
previous close, so all 52 weekends of the seven majors read as a gap of exactly 0, and the
collapsed yardstick read early 2013's weekends as 30–72σ. 350 of the 364 were mended from
Dukascopy's own gap, spliced onto the stored Friday, where its Friday and Sunday closes are
within 10 bp of the store's; 14 were refused. Every other
pair weekend opening exactly at Friday's close is left unscored (359 of 14,705): USD/INR's
and USD/KRW's 2020-2025 hold about 60 each, which no free source can mend, and scored they
read the next real weekend as hundreds of sigma (USD/INR 2022-01-09: −389σ under a
two-year window).

**Futures are read one contract at a time.** Yahoo's continuous series interleaves months.
The softs' history is Dukascopy's single-contract CFDs from 2018, spliced at a measured
seam; live cattle keeps Yahoo's series minus its interleaved stretches. The night across a
roll is not scored, and a bar under 5% of usual volume is a quote, so a hole.

**Live cattle rolls when its traders do, and its history was judged against the
contracts.** Held to its last trading day, the series sat on a contract hardly trading:
October 2026 traded 3,097 contracts on 10-07 against December's 18,822; October 2025
moved +2.2% one night on 213 while every other contract was flat. December's volume passed
October's on 2026-09-11..16, and twelve business days before the delivery month puts the
roll on 09-15, the day Yahoo's own series moved. Yahoo's series had also mixed two months
(2025-04-09: a +2.8% night and a −2.9% first hour; the contracts −0.3% and −0.2%). Judged
against the single contracts Yahoo still serves, 12 of cattle's 20 flagged readings were
not seen; events went 15 → 8 over 2024-05..2026-10, 6.2 → 3.3 a year against the
agriculture block's median of 3.3 (3.5 before), the rest of the basket unchanged. Rejected:
a cattle ETC (WisdomTree's in London, 3.8 bars a day, a third without volume, daily moves
correlated 0.54–0.63 with the futures) and dropping cattle. Its old import is not to be
re-run: under this roll most of every cycle reads as Yahoo lagging and would be a hole.

**The LME's metals are the LME's own, from Sina**: the right instrument with a short record
(from 2026-07) over a longer record of another (Shanghai tin, Kitco, COMEX aluminium).

**Every coin is its USDT pair on Binance**: one exchange, one quote currency. Below a 2018
seam, before USDT left the dollar, the record is the deepest dollar exchange passing the
splice gates, scaled to meet without a step.

**A provider that does not answer twice in a row is stopped for the run.** An unanswered
request costs about 96 s of timeouts and retries (186 s at Alpaca), and the job has 20
minutes. Asked one by one, a silent Yahoo's 38 instruments and 133 dividend lookups come to 274
minutes on a simulated clock (2026-10-05); stopped after two, to 3. A cancelled job
delivers nothing and tells no one. One silence can be a blip; two in a row is the provider.
A wrong stop costs its instruments an hour, and the health chat names it. A refusal (404,
400) is an answer and stops nothing.

**An open month is repaired by hand, inside the hourly run.** Only the hourly run saves
the open months to the release; a second workflow writing them could finish around an
hourly save and erase one or the other's bars. So a repair is an input of the hourly
workflow and runs in its slot. By hand, not every hour: holes are rare (one since the
switch) and the stale check names a fund that falls behind; filling them unasked would
mix a second feed into a store whose provider was chosen by measurement.

**A missing or refused key costs its provider, not the run.** One provider's key used to
stop the whole fetch before anything was asked, for all 173 instruments, and only a
failed-run streak said so three hours later. Its instruments are skipped, the others
fetched, and the health chat names the key every run, since none of them is fetched until
it is fixed.

**A refusal is told by what it costs, not when it happens.** A lone Yahoo 429 (cotton at
03:05 UTC on 2026-10-06, the only Yahoo instrument due) costs its instruments one hour,
fetched again next run; told every time, a day-long block would be a message an hour.
So it is logged, and its instruments stay in the stale check, named once one is behind
past its limit (a fund after 7 session hours, a future after two sessions). A spent
Tiingo or SiftingIO budget is still told every run: that is a quota gone.

**USD/BRL trades in its own session**, 09:00–18:00 São Paulo (`b3_fx`): outside it real
moves are under 3.5 bp an hour. B3's holidays are outside it too: on them SiftingIO's
quote sits flat, or moves up to 23 bp an hour on thin offshore quotes, against 14 bp on a
trading day (2019-09 to 2026-10). Scored, they made a session of their own and a night
after it.

**A provider down is that provider's, not the run's.** The run goes red only when the fetch
got nothing at all. A red run for one dark provider said nothing the health chat had not
said, and every hour of an outage would read as a broken bot.

---

## The sources' vote

**The sources vote, and the store's provider is one vote that saw it.** A real trade
shows on other feeds; a bad print does not. "Confirmed if any feed saw it" let one other
feed keep a bad print; "every feed must agree" would let one feed's own bad tick take a
real move out. A majority of every feed that has the hour weighs them equally. Over the
30 days to 2026-10-08, of 444 readings at 4σ or more: 436 real, 3 uncertain, 1 not real
(GIGB 09-30, −0.32% against Sina's and MarketWatch's +0.09%), 4 single source; the
detector's flagged readings over all history unchanged (17,088), as were their words.

**A move not real is not scored, and a sent message is marked, not deleted.** Kept out of
the yardstick, a bad print cannot inflate the next half-year's σ. Marked rather than
deleted, a wrong vote costs a line, not a real move.

**A tie is uncertain, and stays scored** (the user's choice, 2026-10-08). Nothing tells
which side of a tie is wrong, and more sources are to be found. The cost, measured: with
one other source every disagreement is a tie, so cattle's and the softs' bad prints stay
in. LE=F's 2026-10-01 open (−1.82%, the October contract's bars beside December's) was
not seen and left out; it is now uncertain and scored, though below the bottom word. Of
the 232 not seen on record, 225 had one other source; they stay not real until the replay
counts them again with every source that reaches them.

**A vote is counted again once a day while its closest source still reaches the hour.**
Sources correct their bars, and the vote of a move's first hour can be wrong the next
morning. Past the closest source's reach the further ones would vote alone - Yahoo's two
years against MarketWatch's nine days - so the vote stands from then: 9 days for funds,
pairs and cattle, 29 for coins, 79 for the softs. The record keeps a vote 90 days, longer
than any recount, so an old one is not dropped and counted afresh every run.

**A provider's corrections come in through its ordinary request.** On a run with a vote
due, the hourly fetch reaches back to that vote's hour, and the store takes every bar it
brings back as it takes the last three hours': in an open month the fresher copy wins.
A separate request per vote would spend Tiingo's and SiftingIO's allowances, which the
live run needs; one longer answer costs nothing more. Taking only the voted hours would
need a rule per provider: Sina's whole ~78 days and Yahoo's last day are already taken
on every run. Measured 2026-10-08: Binance revised no bar in 29 days (0 of 10,672 hours),
Yahoo 472 of 1,290 fund hours in 9 days, 2 by more than 1 bp.

**The vote is said in a few words under the move.** `❌ not real: only Binance had it
[1/3]`, `⚠️ uncertain: seen by Yahoo, Alpaca [2/4]`, `single source: only SiftingIO had
data`, `single-source asset`; a real move says nothing. Who had it and how many voted is
what a reader needs to weigh it.

**A source's answer is one question: did that feed move with it?** An hour of lag is
allowed, a missing hour bridged. Which feed was wrong is not asked: sources lag, miss
hours (no USD/KRW bar on Seoul's martial-law night, a real +2.5%) and print their own bad
ticks, and every culprit rule breaks a real case.

**Bars around the move outweigh a bridge across it.** A bridge only says the price got
there somehow.

**Sina's softs are not asked across their own change of contract.** Sina's continuous
series hold another month for days at a time (coffee 2026-08-03..10 about −500 bp from the
store, cocoa 06-16..22 +220 and 07-21..08-06 −250, against ±10 bp on the same month). Inside
such a stretch each feed is still compared with itself; a move across its start or end is
unknown. Found as a step of 50 bp or more in the session median offset, which a one-hour
bad print cannot make. Over 2026-05-11..10-06, 36 moves judged, none crossed one.

**Every source of the class is asked, except the instrument's own provider.** More
independent feeds catch more bad prints, and a vote counts heads, where a third feed
breaks a tie. This replaces "a fund is asked of two feeds, not three" (2026-10-07), which
weighed only what a third adds when any one feed's word confirms. One list
(`verify.SOURCES`) names each source once, with its reach as measured, so a new feed is
one line and no reach is stated twice: Yahoo's had been 55 and 700 days in code against
59 and 729 measured (2026-10-08). Of the 173 instruments, 107 keep their sources; 66 funds
served by Alpaca, Tiingo, Twelve Data or Google gain MarketWatch as a third; none loses
one.

**The same price from two vendors is agreement, not a copy.** A vendor that prints the
store's bars exactly is still a second voice: vendors of one exchange tape print identical
bars on many hours. Over the 30 days to 2026-10-08, against the funds' stores, Yahoo matched
open, high, low and close on 0.13 (Tiingo's) to 0.89 (Sina's, Twelve Data's) of hours and
Sina on 0.24 to 0.87 (medians by the store's provider); for the pairs and coins, whose
vendors each have their own prices, 0.00. A rule that took identical bars for a copy dropped 11 honest fund votes in those 30
days, and was taken out. Only the provider an hour came from is not asked about it,
because its word is the store's: known by name, not by comparing bars.

**A first hour the feeds saw happen overnight is moved into the night.** A fund's first
print is sometimes a stale one at the previous close (in 2020-22, 7-12% of fund-days
against Yahoo's daily opens), and the night's move then reads as the first hour's: TLH
2020-03-09, +3.8% in the first hour and a gap of +0.03%, against +4.65% overnight on the
tape. Marked "not seen", the real move would be gone. When no feed saw the hour move but
every feed saw the move from the previous close, the night takes it at the feeds' own
night. The night ends where the market really opened, measured where it matters.

**The history is judged once, by the same rule, from sources that reach it.** A broken
print older than the live sources' reach was never asked about, and sat in its
instrument's yardstick for the next half-year (USD/PLN 2022-07-31: two flat bars 15% off,
an "extreme" weekend). Only another feed counts: a stretch the source itself supplied to
the store is skipped, not confirmed. Over the full history, flagged readings went 17,257
→ 17,112 and events 12,969 → 12,850; pushes over the year to 2026-10-01 went 210 → 207.

**A shifted fund day is cut only when the store contradicts itself.** Yahoo's
daily closes, the only source that reaches the funds before 2016, were compared over the
whole record, and one feed's daily close is no referee on its own. 108 fund-days closed
far from Yahoo's; 32 were off all day; 14 of those were
a round trip in the store (in, and back out the next session) while Yahoo's closes stayed
flat, and their bars were removed. The other 18 were left: Yahoo's own errors (CPER
2016-01-19, DBE 2021-04-06, PCY 2008-09-29) or real crisis days (RWR 2008-12-01). Over the
full history, flagged readings went 17,112 → 17,100 and events 12,850 → 12,837; 13 old
readings went (LQD 2006-10-02 among them), one appeared (EWU 2008-10-10, a night at
−6.2σ, as the yardstick lost a false day); this week unchanged.

**A removed bar is kept as its hour with no price, not deleted.** Deleted, the 14 days were
holes that `fill-gaps` listed, all 14, to re-ask Twelve Data, the vendor that served them.
Kept with no price, the hour is held: a merge never fills it, `fill-gaps` does not list it,
and the gate drops it like any invalid bar. The store remembers in itself, with no table of
bad days beside it. The gate had let a bar with no price through (none was stored, over the
173 instruments, 2026-10-08); it now refuses it first.

**A dividend refresh keeps the splits already declared.** The full Tiingo refresh rewrote the
whole table, and the morning check never asks back past a fund's checked-through date, so a
split Tiingo did not list (or any split, from Twelve Data, which cannot see them) would have
been lost for good, and its day's gap scored again.

**Candidates are the detector's own readings at ≥ 4σ.** A check with its own σ missed 23
of 543 flagged readings after a calm stretch.

| instruments | verified against | why |
|---|---|---|
| FX pairs | Yahoo and MarketWatch | MarketWatch has every hour (Yahoo 21–72%), 0–0.6 bp off |
| funds | two of Yahoo 30-minute, Sina 30-minute and MarketWatch hourly, neither the fund's own | Sina matches the tape to 0.0 bp; two, so that one feed cannot say a move did not happen |
| coffee, cocoa, cotton | Sina global futures | 0.2–2.2 bp off, hourly correlation 0.96–0.99 |
| live cattle | MarketWatch continuous | hourly correlation 0.92 |
| coins | Coinbase and Kraken | other exchanges' dollar pairs: hourly moves correlate 0.988–1.000 with Binance's (300 hours to 2026-09-25); a year of checks found 4 moves not seen, all Binance's own on 2025-10-10's liquidations |
| LME metals | none | no independent free feed |

---

## The repository

**Derived data is not tracked**: it rebuilds from the bars in under a minute.

**A one-off tool is deleted once its work is done.** The futures', coins' and pairs' history
builders wrote whole stores, so a rerun would have undone every later fix (cocoa's
2026-07-21..08-10 among them); the feed probes spent quota (one about 100 SiftingIO calls);
the history check decided "the store's own feed" once per fund-year and left 85% of the
funds' flagged readings unasked. Git keeps them (deleted 2026-10-08); what they established
is in this file.

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
| a longer window for weekend gaps (pairs 1 or 2 years, funds 1 year, a fund's weekends pooled with its nights) | once the pairs' stitched zero weekends were mended or unscored, the pairs' median QLIKE went 3.63 → 3.41 (1 year) and 3.34 (2 years), each better for only 11 of 16; funds 2.39 → 2.34, better for 61 of 133. The gain is small, real shocks read quieter (EUR/USD 2022-02-27 extreme → noticeable at 2 years), and one window for every instrument stays the rule |
| every fund's night from Yahoo's daily official open | one feed's opening print is no referee: LMBS 2026-10-05 "opened" at 48.60 on Yahoo, Sina and MarketWatch alike, while its first minute traded at 48.38 over a 48.50 close; the feeds carry the same tape, so a vote on the opening price picks the outlier |
| a stale first print found from the bar alone (open at the previous close, an extreme of its bar, a big first hour) | against the official open, it caught at best 7% of stale prints at 66% precision |
| a third external feed for funds | 6 of 322 large fund hours split the externals; two already decide them |
| a bridge may confirm, never reject (or no check in the second feed's missing hours) | over 2024-10 to 2026-10, frees one real hour (USD/KRW 2024-12-03 15:00, not flaggable) and lets in 34 flags, almost all USD/INR rollover ticks at 21:00–01:00 UTC; the narrower "unless it moved further the other way" changes 2 verdicts and 1 flag each way |
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

**Repository size**, about 890 MiB as GitHub counts it (915,487 KB on 2026-10-06), mostly
superseded Parquet in history; GitHub warns at 1 GB. To act: a history rewrite, which force-pushes the live branch irreversibly.

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
