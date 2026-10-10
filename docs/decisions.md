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
about ten times rarer, so levels √2 apart make each word about three times rarer to reach
than the one below: events at or above 6, 8.5, 12 and 17σ were 12,438, 4,195, 1,301 and
479 over all history (2026-10-09, the funds' first bars mended), 3.0, 3.2 and 2.7 times
apart. Extreme has no ceiling, so as a band of its own it is only about twice as rare as
major's (479 against 822). The 6σ bottom is a volume choice: moves of every size hold at the close alike
(76–80%), so a higher bottom buys fewer messages, not better ones. At 6σ: about 9 messages
a week (8.6 over the year to 2026-10-01, the delivery replayed hour by hour); for one
instrument an event at each word about every 4 months, 11 months, 3 years and 5½ years
(over the 2,602 settled instrument-years to 2026-10-08).

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
a year. About 4 pushes and 10 note rows a week (209 and 546 events over the year to
2026-10-01).

**The week turns at the run after its last NYSE close.** Every move is checked at the next
NYSE close, so a week ending just after it holds all its checks. The economic calendar is
its own message, sent just before the note: a forecast and a report are read differently.

**The open week is curated; earlier weeks are history.** For the open week the channel
says what the detector says now. Once the next note opens, nothing of the old week moves,
so a cold rebuild cannot rewrite a closed record or post its difference as new alerts.

**The note is read by block, in time inside** (the user's, 2026-10-08). Its rows stand in
the basket's blocks, in a fixed order, each under a name-line with an icon (`━━━ 🏛 RATES
━━━`), so what moved together is read together: the FOMC hour of
2026-09-16 18:00 had rates, credit, gold and FX rows in one run, which by time alone read
as one list. Time order stays inside a block, since a note is a record. The icons avoid 📈
and 📉, which say a row's direction. A long note is cut between blocks, keeping each whole
in one message; only a block longer than a message is cut, and says "continued". The
first message's room is what the header leaves, so the header never stands alone. Over
541 weeks since 2016 (the detector's output of 2026-10-08), 495 notes fit one message and
11 cut a block.

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

**A pair against the US dollar is named by its other currency** (the user's, 2026-10-09).
"Dollar / Turkish lira" spent words on what every reader knows; the ticker before the name
(`USD/TRY`) still says which way the pair is quoted, so +2% is the lira weaker. USD/BRL
already read "Brazilian real". A fund keeps the word where it is the point: EMB's
"Emerging-market sovereign bonds in dollars", against EMLC's in local currencies.

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
retroactively with every dividend. Payouts and splits are read as declared - Yahoo's every
morning, Tiingo's daily endpoint on a full refresh - since a ratio of adjusted to
unadjusted cannot see a split.

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

**A USD/BRL night that opens exactly at the previous close is no measurement either** (the
user's go, 2026-10-09). Its history is Twelve Data's, which stitches opens as USD/INR's and
USD/KRW's: 22 of its 1,701 scored nights to 2026-10-08 opened exactly at the close, and on
such nights Yahoo's BRL=X moved like on any other (median 10.4 bp against 11.2,
2024-10..2026-10). Unscored, one flagged 2023 night goes over all history; the year to
2026-10-01 is unchanged. Not the futures': live cattle's zero nights are real (Yahoo's
official gaps agree), and the softs' carry no bigger first hour than other nights. Nor the
funds': since 2023, 98% of their zero gaps match the official open within 5 bp.

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
(from 2026-04-22, Wallstreetcn's below Sina's) over a longer record of another (Shanghai
tin, Kitco, COMEX aluminium).

**Wallstreetcn for the LME**: their voter, and the history below Sina's. Its chart
endpoint (api-ddc-wscn.awtmt.com) serves the three-month metals hourly, without volume,
about 170 days back, and a GitHub runner gets the same bars as the development machine
(2026-10-09). Against Sina's stored bars from mid-July to 2026-10-08, the closes are
identical in 53–68% of hours and 0 bp apart at the median, hourly moves correlated 0.961
(tin), 0.990 (nickel) and 0.992 (aluminium): in part Sina's upstream, so a second voice
rather than an independent feed - and the only one found (Eastmoney refuses runners; the
rest are under "Rejected"). As a voter it saw all 16 LME readings at 4σ or more from
2026-07-15 to 10-08 on the store as it was, and all 9 on the store with its history
below. Its votes are counted again for 79 days, as the softs', not its whole reach: the
record keeps a vote 90 days (`verify.KEEP_DAYS`), and further back it would vote on
its own bars below Sina's.

Its bars of 2026-04 to 07 hold openings off the market: 11 sessions open 1% or more off
the last close and are back as far within the hour (aluminium 8 of 59, tin 3 of 54,
nickel none of 59; none in Sina's bars or its own since). Imported, aluminium's of
2026-05-15 and 06-01 read as major moves. They are left out at import, holes rather
than bars (`backfill.opening_misprints`): they are not the store's yet, and nothing is
rewritten. The rest passed the store's gate in the backfill's run of 2026-10-10 (hourly
correlation 0.962–0.992, median 0.00 bp, about 1,100 shared hours) and is 910, 1,047 and
1,040 hours from 2026-04-22; no stored bar changed.
Rebuilt with it as of 2026-10-08 21:05: no reading in the new months, none outside the
LME changed, and the LME's flagged readings since July go from three to one - nickel's
2026-09-02 hour (6.3σ) and aluminium's 09-17 night (7.4σ) fall under 6σ against the
fuller yardstick, aluminium's 09-10 hour stays (7.6σ → 6.2σ). All three were note rows;
no push changed. Its half-year yardstick fills about 2026-10-22 instead of 2027-01.

**Wallstreetcn on cocoa, cotton and the pairs**: a second voter for cocoa and cotton, a
third for 16 pairs (it does not carry USD/KRW). Its quotes are its own: over 2026-04-22 to
10-09 at most 2% of its closes equal the store's (EUR/USD 60 of 2,938, cocoa 2 of 996,
cotton 5 of 2,120). Cocoa is 7.0 bp off at the median, hourly moves
correlated 0.995; cotton 2.4 bp and 0.975. On 17 of cocoa's 100 days and 7 of cotton's 119
it sits more than 50 bp off - its own changes of contract, which the vote treats as Sina's
(`own_rolls`, `verify.switches`). The pairs, over the same months, are 0.8–3.4 bp off at
the median, hourly moves correlated 0.92–0.98, but 0.23–0.81 for USD/TRY, USD/CNH, USD/NOK
and USD/INR, whose quiet hours are mostly quote noise. Over the pairs' 168 readings at 4σ or
more from 2026-07-12 to 10-10, it and Yahoo answered alike on 145 of the 146 where both had
bars around the move; where Yahoo had none (22, all but one in the week's opening hours,
Sunday 21:00 to Monday 01:00 UTC, of USD/CNH, USD/NOK, USD/TRY and USD/INR) it had bars on
15. Replayed on the live store and record of
2026-10-10 05:06, every reading inside its recount (9 days for the pairs, 79 for cocoa and
cotton): it saw 39 of 40, all 8 of the softs', and one vote turns - USD/BRL 2026-10-05
12:00, a pushed major, from real to uncertain: SiftingIO and MarketWatch put the fall after
Brazil's election in B3's first hour, Yahoo and Wallstreetcn before its open. No reading
is added, dropped or reworded.

**The FT for live cattle and coffee**: their second voter. The FT's markets pages chart
from markets.ft.com's chartapi/series: hourly bars by an instrument's xid, the last
24 trading days, each dated by its end in UTC; a GitHub runner got the same bars as the
development machine, to the cent (2026-10-10). Live cattle it carries by contract
(LCZ26:CME), and it is asked for the one the store holds: its continuous series held
October while the store held December from 2026-09-15, 35–170 bp apart. On the store's
December days to 10-09 the contract sits 2.3 bp off, hourly moves correlated 0.856 -
cattle's hours move a few bp, mostly noise - and its closes equal MarketWatch's in 26% of
hours: in part one upstream. Coffee it carries only continuous (KC.1:IUS), changing
contract a week after the store in 2026-09, with no bars from 09-15 to 09-18; on the
store's month (09-21 to 10-09) 3.6 bp off, hourly moves correlated 0.988, 1% of its
closes equal to Sina's. Of the store's largest hourly moves from 2026-09-10 to 10-09 (the
top tenth) it saw 10 of cattle's 11 and 19 of coffee's 20, the rest outages, and denied
none; MarketWatch, nine days deep, could answer on 2 of cattle's. Coffee's recount falls
to 30 days, the FT's reach; cattle's stays MarketWatch's 9. Replayed on the live store and
record of 2026-10-10 05:06: no cattle or coffee reading inside its recount, so no vote
changes, and coffee's two of 2026-07-28 and -29 stand as voted, real. Its cotton (CT.1) is
not asked: cotton has two voters.

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
30 days to 2026-10-08, of the 242 readings at 4σ or more inside their recount: 240 real,
1 uncertain (LE=F's 10-01 open), 1 not real (GIGB 09-30, −0.32% against Sina's and
MarketWatch's +0.09%); the detector's flagged readings over all history unchanged
(17,088), as were their words.

**A move not real is not scored, and a sent message is marked, not deleted.** Kept out of
the yardstick, a bad print cannot inflate the next half-year's σ. Marked rather than
deleted, a wrong vote costs a line, not a real move.

**A tie is uncertain, and stays scored** (the user's choice, 2026-10-08). Nothing tells
which side of a tie is wrong, and more sources are to be found. The cost, measured: with
one other source every disagreement is a tie, so cattle's and the softs' bad prints stay
in. LE=F's 2026-10-01 open (−1.82%, the October contract's bars beside December's) was
not seen and left out; it is now uncertain and scored, though below the bottom word. Of
the 232 not seen on record, 225 had one other source; they stay not real, since history is
not voted again (below).

**A vote is taken when the move is found, then at each end of its market's session,
while its closest source still reaches the hour** (the user's schedule, 2026-10-08).
Sources and the store's own provider correct their bars, and a session's end is when
the day's are final: the NYSE close for funds, each daily-session market's own close,
00:00 UTC for coins and pairs, which never close. Past the closest source's reach the
further ones would vote alone - Yahoo's two years against MarketWatch's nine days - so
the vote stands from then: 9 days for funds, pairs and cattle, 29 for coins, 79 for the
softs. The record keeps a vote 90 days, longer than any recount. Against voting every run
of a move's first day, it asks a mean of 7.7 requests a run instead of 34.7 (the 30 days
to 2026-10-08, simulated).

**A provider's corrections come in through its ordinary request.** On a run with a vote
due - the first after a session's end - the hourly fetch reaches back to that vote's hour,
and the store takes every bar it brings back as it takes the last three hours': in an open
month the fresher copy wins. A separate request per vote would spend Tiingo's and
SiftingIO's allowances, which the live run needs; one longer answer costs nothing more.
Taking only the voted hours would need a rule per provider: Sina's whole ~78 days and
Yahoo's last day are already taken on every run. Measured 2026-10-08: Binance revised no
bar in 29 days (0 of 10,672 hours), Yahoo 472 of 1,290 fund hours in 9 days, 2 by more
than 1 bp.

**The vote is said as each source's own move** (the user's wording, 2026-10-08): `❌
Binance(+2.03%), Coinbase(-0.10%), Kraken(-0.10%)` for a move not real, `⚠️ Yahoo(-2.50%),
Alpaca(-2.50%), Sina(outage), MarketWatch(-0.50%)` for a tie; a real move, or one no
other source carries, says nothing. The numbers let a reader weigh it without a key.

**An outage is left out of the count** (the user's choice, 2026-10-10; from 2026-10-08 it
counted against the move). A source with nothing to show about a move - down, no bars
around it, no bar after it yet, or across its own change of contract - says nothing
either way: it is left out, and still named on the line, `Sina(outage)`. Counted against,
one source down turns a clear vote into a tie (a pair's two to one into two to two), and
the more sources an instrument has, the more often one is down. When none can answer,
nobody could check the move: it is uncertain - scored and alerted, every source
`(outage)` on the line - neither the store's word alone nor held back, and the next count
after they are back decides. On the record of 2026-10-10 07:06 no verdict changes: of
the 301 votes the old rule reproduces, 3 had an outage - single-voter USD/INR votes of
2026-10-04's check, uncertain either way. A record from before the vote still reads its
unknowns as they were counted then.

**A vote is taken with what the sources serve at the time.** No waiting for a source's
next hour: one that has not shown the move yet counts against until the next count, at
the session's end. Over the 30 days to 2026-10-08, the vote at the run that found each of
the 250 moves inside their recount matched the vote on the whole bars: no real move would
have been held back.

**A source's answer is one question: did that feed move with it?** An hour of lag is
allowed, a missing hour bridged. Which feed was wrong is not asked: sources lag, miss
hours (no USD/KRW bar on Seoul's martial-law night, a real +2.5%) and print their own bad
ticks, and every culprit rule breaks a real case.

**Alpaca's consolidated tape votes on every fund, the ones Alpaca serves included** (the
user's choice, 2026-10-08). The store has 30 funds from Alpaca's IEX feed, one exchange's
trades; the tape is every exchange's, and shows where the market traded while IEX's thin
bars can stray - IEX's typical error is what it catches. Named apart, `Alpaca_IEX` and
`Alpaca_SIP`. The free plan serves the tape fifteen minutes behind, so in a move's hour it
cannot serve the hour whole: a late source is not asked until it can, and votes from the
session's end on - neither a yes nor an outage before. Over the 30 days to 2026-10-08,
simulated: 9.2 requests a run against 7.7, the tape's 704 over 460 runs, at most 67 in one.

**History is not voted again** (the user's, 2026-10-08). Older than its recount (9, 29 or
79 days), a move's vote stands as the record holds it; a move never voted counts as
normal. A replay of the live vote over all history was built and tried, and dropped: with
one other source the vote cannot call a move not real - the store's yes against one no is
a tie - and most of history has one at most (the funds' Alpaca tape from 2016, the pairs'
Dukascopy before Yahoo's two years, one contract series for coffee, Bitfinex for BTC in
2013-14). There it could only take known bad prints back. On six instruments (run
37833953643): USD/TRY's 33 not real became ties - 2023-05-26 +0.70% then -0.67%, Dukascopy
+0.01% both - and the funds' 116 not real and overnight, all the tape's, would have too;
it added not-real votes only where two sources reach (15). HF Data, planned as the funds'
second source to 2022-03, had withdrawn everything before 2022-03-07 on 2026-10-03 ("the
dataset now holds the IEX Exchange HIST segment only"). Do not reopen without a second
source for those stretches.

**A source sees a move at half its size, and no source is shifted** (measured 2026-10-08,
the user's choice). 3,444 moves of 4σ or more, each put to the free sources at their full
reach (Yahoo's pairs two years, Coinbase one, the funds' sources 9 to 77 days): of 3,704
answers, 98% moved at least 0.8 of the stored move or under 0.2 of it, and the emptiest
band was 0.5 to 0.6. Bad prints on record need the line at 0.3 or more - Binance's FIL wick
of 2025-10-10, which Coinbase moved 0.24 of; USD/INR 2024-12-17, Yahoo 0.15 - and the real
moves on record pass up to 0.9 (1.03 to 1.62: the FOMC hour, Seoul's martial law, USD/TRY
printed an hour late). Between 0.3 and 0.7 the line turns few votes: alert-level pairs tied
86, 93 and 98 times. Two sources that both answered agreed on 97 to 100% of moves at every
line from 0.2 to 0.9. No source carries the move an hour after the store's provider as a
rule: Yahoo's pairs in the same hour 94% of the time (an hour later 2.7%), MarketWatch's
90% (31 moves), Coinbase and Kraken 91 to 93%, the funds' sources 97 to 100%; the hour of
lag allowed either side covers the rest.

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
because its word is the store's: known by name, not by comparing bars - the
instrument's provider, and a source that mended the store by hand inside the recount
(`verify.SUPPLIED`: Yahoo, TUR's holes of 2026-10-01 and -02).

**A first hour the feeds saw happen overnight is moved into the night.** A fund's first
print is sometimes a stale one at the previous close (in 2020-22, 7-12% of fund-days
against Yahoo's daily opens), and the night's move then reads as the first hour's: TLH
2020-03-09, +3.8% in the first hour and a gap of +0.03%, against +4.65% overnight on the
tape. Marked "not seen", the real move would be gone. When no feed saw the hour move but
every feed saw the move from the previous close, the night takes it at the feeds' own
night. The night ends where the market really opened, measured where it matters.

**The history was judged once (2026-10-07), from sources that reach it, and its votes
stand.** A broken print older than the live sources' reach was never asked about, and sat
in its instrument's yardstick for the next half-year (USD/PLN 2022-07-31: two flat bars 15%
off, an "extreme" weekend). Only another feed counted: a stretch the source itself supplied
to the store was skipped, not confirmed. Over the full history, flagged readings went
17,257 → 17,112 and events 12,969 → 12,850; pushes over the year to 2026-10-01 went 210 →
207. It is not run again: "History is not voted again" (above).

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
173 instruments, 2026-10-08); it now refuses it first. Those 14 days stay so; a bad bar
found since 2026-10-09 is a row in the vote record instead (below).

**A bad bar found later is a row in the table, not an edit to the bars** (the user's,
2026-10-09). The vote record (`data/jump/verified.csv`) already carries every live verdict,
and the detector reads it each run: not real leaves a move out, overnight moves it into the
night. A row costs about 90 bytes of plain text (the record: 589 rows, 53 KB), where the
funds' first-bar mend added 22.6 MB of Parquet that git keeps for good; a row is undone by
deleting it, and no fetch can overwrite it. The repairs made before stay in the bars
(manual, section 4), and the code that could rewrite a stored price (the backfill's
`repair-alpaca`, `bars.remove`, `merge`'s `revise_settled`) went the same day. This does not reopen "History is not voted again", which is about
re-running the vote over all history: a single bad bar found later, with the source that
shows it, is recorded as its row.

**The funds' wrong opens of 2016–2022 were mended from the tape.** A fund's first bar
often did not open where the market did, most of all in Twelve Data's history (2020-02 on):
at the previous close, so the night read 0 and the first hour carried it (on STIP's
distribution mornings the payout, added back to a zero gap, read as a +34σ night), or 15 bp
or more off elsewhere (IHI at its pre-split price on 2021-07-19; UNG next to the previous
close on 2021-11-29, when it opened 9% lower). Against Alpaca's tape (`tools/tape_bars.py`),
on fund-days whose two closes agree within 10 bp, 9,783 first bars took the tape's open,
high and low, the stored close kept (`tools/tape_mend.py`): 2,918 stale; 2,154 whose stored
open the tape never traded at in that half hour; 1,080 at the tape's high or low, a stray
trade taken for the open (VCSH 2021-11-05: 81.58, against an open of 82.02 and a close of
82.03); 3,631 where both opens are ordinary trades of the half hour. 473 are from before 2020.
Which open is right was judged apart from both: the fund's night predicted from its eight
most similar funds' nights that morning, where their store and tape agree. With the stale
mornings as the yardstick, at the same distance between the two opens, the tape's is the
right one on about 88% of the never-traded mornings, 92% of the high-or-low ones and 85% of
the ordinary ones (94% in the equity funds). In the bond funds the ordinary ones came out
even (51% of 111 in credit, 49% of 27 in rates), so those 207 were left. A mend also needs
the official open within 5 bp of the tape's, and the tape's open inside the store's own
first bar: the official open is taken from the same tape and repeats its stray prints (CPER
traded once at 13.01 on 2016-08-22, then at 14.10 all hour), and taking Yahoo's daily open as
the night without that guard flagged 772 of the stale mornings themselves (PFF 2007-06-27, a
+21.6% night). Over the full history (bars to 2026-10-08, rebuilt cold), flagged readings
went 17,101 → 16,652 and events 12,843 → 12,436 (high and up 4,320 → 4,195, extreme 490 →
479): first hours that carried a night (EWQ 2020-03-12 −8.9%, EEM 2022-03-16 +4.9%, XLC the
morning after Meta's results, 2022-02-03) are nights now. The year to 2026-10-01 is unchanged
(755 events, 209 pushes), and no reading since 2023 changed its word. Not reached: before
2016 (the tape starts there), and the fund-days whose two closes disagree (below). The
hourly run does not see a revised old bar, so the note in `config/basket.yaml` that records
this is the edit that rebuilds the metrics once.

**From 2023 the funds' opens are left as stored** (the user's, 2026-10-09). Against the tape
to 2026-10-08 the store is far cleaner there: 11 to 23 stale opens a year, the two closes
apart on 2% of fund-days. 1,242 mornings pass the same rule, the tape's open the right one
on about 92% of them by the same referee, but mending them moved the year to 2026-10-01
from 755 events and 209 pushes to 749 and 206, and nothing in the current week. And it
would not stay mended: the IEX feeds that serve 57 funds live take a morning's first bar
from one exchange (Tiingo's funds had 26 such mornings in 1,000 over 2026-07..09, against 5
to 20 before), and the live vote answers them where it matters, at 4σ and up.

**The funds' 2016–2019 price levels are left off the tape's** (the user's, 2026-10-09: the
history is left alone). Hour by hour, the store's close is more than 10 bp from the tape's on
25% of in-session hours in 2016, 16% in 2017, 12% in 2018, 6.5% in 2019 and 1 to 3% in
2020–22; where the two differ, Yahoo's official close sides with the tape 72% to 7%. It is
a steady offset (680 fund-months: LQD about 1.0% and EMB 1.5% high in 2016, REM 7% in
2017-12) that steps on payout days, by about one payout (LQD and EMB on 2016-09-01, 11-01,
12-01 and 2017-05-01): there the store's price did not drop and the payout added back to the
gap reads as a move. An offset alone moves no reading; its steps flag REM's ex-dividend
nights of 2016–2019 (four, three of them high). Mending it rewrites whole years of 86 funds
for a handful of readings the channel never shows again.

**A dividend refresh keeps the splits already declared.** The full Tiingo refresh rewrote the
whole table, and the morning check never asks back past a fund's checked-through date, so a
split Tiingo did not list (or any split, from Twelve Data, which cannot see them) would have
been lost for good, and its day's gap scored again.

**Candidates are the detector's own readings at ≥ 4σ.** A check with its own σ missed 23
of 543 flagged readings after a calm stretch. They are scored on both yardsticks: the raw
one, and the detector's, with the moves voted not real left out and the nights moved - a
move far only once a bad print left the yardstick is flagged, so it is voted on too. 86
flagged readings over all history were far only that way (2026-10-08); over the 30 days
to then, 2 of 446 candidates. Such a move, voted not real, leaves that yardstick itself
too, so it is scored with itself back in; without that its vote vanished the count after
it was taken and returned the one after (USD/TRY's history flipped four votes every pass).

| instruments | verified against | why |
|---|---|---|
| FX pairs | Yahoo, MarketWatch and Wallstreetcn (not USD/KRW) | MarketWatch has every hour (Yahoo 21–72%), 0–0.6 bp off; Wallstreetcn its own quotes, 0.8–3.4 bp off |
| funds | Yahoo 30-minute, Sina 30-minute and MarketWatch hourly, but the fund's own, and Alpaca's consolidated tape | Sina matches the tape to 0.0 bp; every source of the class votes |
| coffee, cocoa, cotton | Sina global futures; Wallstreetcn for cocoa and cotton; the FT for coffee | Sina 0.2–2.2 bp off, hourly correlation 0.96–0.99; Wallstreetcn 0.995 and 0.975; the FT 0.988 |
| live cattle | MarketWatch continuous; the FT's bars of the store's contract | MarketWatch hourly correlation 0.92; the FT 2.3 bp off |
| coins | Coinbase and Kraken | other exchanges' dollar pairs: hourly moves correlate 0.988–1.000 with Binance's (300 hours to 2026-09-25); a year of checks found 4 moves not seen, all Binance's own on 2025-10-10's liquidations |
| LME metals | Wallstreetcn | the only feed found that a runner reaches: 0 bp off at the median, hourly correlation 0.96–0.99, in part Sina's upstream |

---

## The repository

**Derived data is not tracked**: it rebuilds from the bars in under a minute.

**A one-off tool is deleted once its work is done.** The futures', coins' and pairs' history
builders wrote whole stores, so a rerun would have undone every later fix (cocoa's
2026-07-21..08-10 among them); the feed probes spent quota (one about 100 SiftingIO calls);
the history check decided "the store's own feed" once per fund-year and left 85% of the
funds' flagged readings unasked. Git keeps them (deleted 2026-10-08); what they established
is in this file. HF Data's client and its backfill modes (`deepen-etfs`, `probe-hfdata`,
`fill-gaps`' second step) went on 2026-10-09: since 2026-10-03 it holds only its IEX
segment, from 2022-03, so they reached nothing. The tape's fetch and the funds' first-bar mend
(`tools/tape_bars.py`, `tools/tape_mend.py`, the backfill's `tape-bars` mode) went the same
day, once the mend was live and 2023 on was measured; so did the Eastmoney probe
(`tools/eastmoney_probe.py`, `eastmoney-probe`), its question answered (below, "Rejected"),
and the Wallstreetcn probe (`tools/wallstreetcn_probe.py`, `wallstreetcn-probe`), once its
bars were taken ("Wallstreetcn for the LME"); so did the FT's (`tools/ft_probe.py`,
`ft-probe`), once it answered the runner ("The FT for live cattle and coffee").

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
| a bridge may confirm, never reject (or no check in the second feed's missing hours) | over 2024-10 to 2026-10, frees one real hour (USD/KRW 2024-12-03 15:00, not flaggable) and lets in 34 flags, almost all USD/INR rollover ticks at 21:00–01:00 UTC; the narrower "unless it moved further the other way" changes 2 verdicts and 1 flag each way |
| holding isolated moves an hour | a third of alerts would be an hour late |
| a watchdog for the trigger's silence | the external scheduler is the party that knows a call stopped |
| Finnhub, Metal Sentinel, FXEmpire, Business Insider | quotes, not bars |
| Financial Modeling Prep, Massive (Polygon) free tiers | daily bars, or today's bars only after the close |
| Eulerpool, London Strategic Edge | no hourly bars for the funds needed |
| Kitco, Shanghai tin, COMEX aluminium, WisdomTree metal ETCs | not the LME's market (hourly correlation ≤ 0.85, or thin) |
| TradingView | ~6,300 bars without a login; its terms |
| Investing.com, the LME, CNBC, Barchart, Boursorama, CME | refuse or forbid automated readers; not bypassed |
| Eastmoney (东方财富) | carries the LME's three-month metals (109.LTNT, LNKT, LALT), but its bar servers refuse machines outside China: from the development machine and from a GitHub runner (2026-10-09) every hourly and daily request, for the metals, ICE cotton and SPY, was cut off or answered without data. Only its live quote and the day's last 1,999 trades reach, too little to rebuild an hour's bar for a recount |
| DailyFX, Stooq | gone; login required |
| Sina forex | six months of hourly bars |
| Sina live cattle | quotes without volume, correlation 0.80 |
| api.binance.com | HTTP 451 from US runners; the market-data mirror is used |
| FXCM history | spliced blind; Dukascopy gates on a measured overlap |
| Interactive Brokers, paid Barchart | a funded account or about $500 a month |

---

## Open questions

Known and left alone on purpose.

**Repository size**, about 921 MiB as GitHub counts it (944,083 KB on 2026-10-09, after
that day's mend of the funds' first bars; 903 MiB that morning), mostly
superseded Parquet in history; GitHub warns at 1 GB. To act: a history rewrite, which force-pushes the live branch irreversibly.

**Undocumented endpoints carry most of the basket** (Yahoo, Sina, Google Finance,
MarketWatch, Wallstreetcn, the FT). A changed shape raises and the health chat names the provider. To act: paid
feeds.

**What else Wallstreetcn carries**, hourly, about 170 days back, without volume (its rank
lists, 2026-10-10), none of it in the basket: sugar (USYO.OTC), lean hogs (LHC.OTC), wheat,
corn, soybeans and soybean oil (USZW, USZC, USZS, USZL), the LME's copper, lead and zinc
(UKCA, UKPB, UKZS), 38 stock indices and index futures (US500, JP225, DE30, VIX, ...) and 52
government bond yields (US10YR, DE10YR, JP10YR, ...). To add one: an entry in
`config/basket.yaml` with its session, a fetch in `jump.backfill`, and its history -
170 days from Wallstreetcn alone, so its half-year yardstick is young for its first months,
with no other voter unless another feed carries it. A yield is a rate, not a price: its
moves would have to be scored in basis points, not as returns, which the detector does not
do.

**History no free source reaches.**

| instruments | missing before | tried |
|---|---|---|
| 55 US funds | 2016-01 | HF Data, Twelve Data, Alpaca's tape |
| live cattle | 2024-05 | Yahoo (730 days), Dukascopy |
| coffee, cocoa, cotton | 2018-01 | Dukascopy |
| LME tin, nickel, aluminium | 2026-04-22 | Sina (last 1,023 bars), Wallstreetcn (about 170 days; taken), Eastmoney (refuses machines outside China) |
| USD/BRL, USD/INR, USD/KRW | 2019-09 to 2020-01 | Twelve Data, Dukascopy, Sina forex |
