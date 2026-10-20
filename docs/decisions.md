# Decisions

Why the bot is built the way it is, so a settled choice is not reopened: each entry is the
choice, then its reason. How each part works is in `docs/manual.md`. The longer story of
each was in the first repository's history, dropped on 2026-10-17 ("A fresh repository").

**Contents:** The detector · Delivery · The basket · Data and providers · The sources' vote ·
The stored history · The repository · Rejected · Open questions

---

## The detector

**An established test, not a home-made one.** The intraday jump test of Lee & Mykland
(2008, *Review of Financial Studies* 21:6): every setting has a published or a measured
reason; a hand-made threshold table has only taste.

**Bipower σ, not a standard deviation.** It averages products of neighbouring moves (L&M
eq. 8), so a jump in the window never pairs with itself. With squares, one 0.05 jump among
0.001 moves inflates σ more than 1.5× (`tests/test_jump_jumps.py`).

**Half a year of calendar time, for every instrument.** Volatility regimes follow the
world's calendar, not a market's hours; half a year is inside the paper's valid range for
every calendar (880 bars for a fund, 4,380 for a coin).

**A young series scores from the paper's minimum** (42 bars for a fund, 78 for a 24-hour
market), marked `young`. A half-year warm-up would have left thirteen funds blind through
March 2020.

**Words at 4.5 / 6.4 / 9 / 12.7σ** (the user's, 2026-10-11). Doubling a move in σ makes it
about ten times rarer, so words √2 apart are each about three times rarer than the one
below (over the 58's history, 9,604, 3,227, 1,056 and 304 events at or above them). The
bottom is a volume choice - moves of every size hold at the close alike (76–80%) - set at
the lowest whose volume is at or below the old basket's. The ladder began at 3.9σ
(2026-09-28) and was 6σ for the 172 (2026-10-04). Measured 2026-10-11 (24-hour events,
pushes at `high` and up, no vote), the year to 2026-10-05 / to 2025-10-05:

| | pushes a week | note rows a week | busiest week's pushes |
|---|---|---|---|
| the 172 at 6 / 8.5 / 12 / 17 | 4.0 / 7.1 | 10.8 / 10.8 | 22 / 151 |
| stage 1 at 3.9 / 5.5 / 7.8 / 11.0 | 6.5 / 6.6 | 15.4 / 9.3 | 21 / 79 |
| stage 1 at 4.5 / 6.4 / 9.0 / 12.7 | 3.7 / 4.5 | 9.9 / 6.3 | 14 / 66 |
| stage 1 at 5 / 7.1 / 10 / 14.1 | 2.4 / 3.4 | 6.8 / 5.0 | 12 / 56 |
| stage 1 at 6 / 8.5 / 12 / 17 | 1.3 / 2.1 | 3.1 / 3.2 | 6 / 38 |

**Rarity is measured, not read off the bell curve.** Against its own half-year σ, an hour
of every class reaches 3.9σ about once in 115, not once in 10,000.

**Gaps are read against their own kind**, nights against nights and weekends against
weekends: clock time does not scale a gap (a 65-hour weekend moves only 1.17× a 17.5-hour
night, French & Roll 1986), and σ borrowed from trading hours predicted the next gap worst.

**A stock index's night runs to its first hour's close** (the user's, 2026-10-11). Its first
value is computed from last closes before most stocks trade, so the night comes in during
the first hour: read as an hour, an ordinary opening gap was a 13σ hour (FTSE 100,
2026-03-03). A fund's first price is a trade, and its night ends there.

**A missing hour is skipped, not stretched**: measured across a hole, three quiet hours read
as one violent one. Only closures the calendar knows are gaps.

**Beyond 1,000σ is a broken print, and so is the stretch after it**, until the price is back
within half of it, at most 24 hours. The largest real readings sit far below (PFF −207σ,
2015-08-24).

**One event per 24 real hours**: a storm of hours is one story, and real hours give every
instrument the same span.

**Rarest since: the last move at least 95% as large, of the same kind and direction**, in σ;
elapsed time rounded down, so the claim stays true.

**Held at the close: one check, at the first NYSE close after the move.** That close shows
a real fade above the noise (79% of a fund's `high` hours half held, against 95% for a
random walk); further out a random walk alone says "gave back" for 14–26% of moves.

**A detector update restarts the week only if it changes the week's events**: the version
hashes parsed code, so a comment does not count.

---

## Delivery

**Rarity is the instrument's; interruption is the reader's.** A word depends only on its
instrument; how often the reader is interrupted is set by which words push.

**No weekly cap**: a detector that falls silent after its third alert fails in exactly the
week records cluster.

**`high` and up push; `noticeable` goes into the weekly note**: two thirds of events, worth
reading in a batch. The week turns at the run after its last NYSE close; the economic
calendar is its own message, just before the note.

**The open week is curated; earlier weeks are history**, so a cold rebuild cannot rewrite a
closed record or post its difference as new alerts.

**The note is read by block, in time inside** (the user's, 2026-10-08), cut between blocks.

**Rarer rings again inside the 24 hours; milder never rings.** A change says so on one line;
after the 24 hours a fix corrects silently.

**The moves one run finds share messages, biggest first, and only the first rings**: news
moves dozens at once (91 events in the FOMC hour of 2024-12-18), and Telegram takes about
twenty messages a minute into a channel.

**Size in σ, word as the colour**: a percentage means nothing where short Treasuries move
0.024% in a usual hour and a coin several per cent. Every move is a price's, in per cent.

**Names**: a pair by its other currency (`USD/JPY · Yen`); an index abroad by its own name
and place (`Nikkei 225 · Japan`); anything else by its ticker.

**A reading is judged only once its bar has ended.**

**Quality is recall on the obvious, not precision**: of each instrument's top 0.01% of
hours, how many are flagged and at which word (`tools/stage_report.py`); precision rewards
delay.

---

## The basket

**Each market in its own hours, each story told once** (the user's, 2026-10-10/11). The 172
came from a paper and saw the world through 132 New York funds: the Nikkei's worst day since
1987 reached it at New York's open. Stage 1 holds 58: 16 stock indices in their own hours
beside SPY and IWM, four sectors, the LME's metals with copper, seven pairs, spot gold and
silver round the clock, four coins. Stage 2 (2026-10-12) puts the futures in place of the
funds that stood in for them - Brent, the 2-, 10- and 30-year Treasuries, corn, wheat,
soybeans, sugar and platinum for BNO, SHY, IEF, TLT, CORN, WEAT, SOYB, CANE and PPLT. Live
since 2026-10-13. The rates block is kept as it measures (the user's, 2026-10-13: "it is
ok"): 31 pushes a year as futures, mostly a data release's 08:00 ET hour and FOMC's 14:00.

**The Bund, the long gilt and TTF join from MarketWatch** (the user's, 2026-10-13: "add
everything good, if something missing - we could try and find later"): the only free live
source of each, about nine trading days back, without volume, so no thin-bar rule. The Bund
and the gilt one contract at a time, their history Dukascopy's ("The stored history"); TTF
as MarketWatch's continuous series, its history EEX's (2026-10-18). The gilt left for its
bonds' fund on 2026-10-18 (below).

**Every instrument is a price, scored and said in per cent** (the user's, 2026-10-17: no
basis points). Japan's 10-year yield, scored in basis points from 2026-10-13, left the basket
with its bars: no free source serves the JGB future hour by hour (Barchart refuses readers,
Databento is paid, SGX gives daily settlements, JPX's quotes are QUICK's and not to be passed
on, Onvista refuses), and the Tokyo JGB funds (2510.T, 2561.T) trade about six hours a day and
moved with the yield at 0.35-0.46, inverted, confirming none of its 17 big moves (2026-09-30
to 10-16). IG's CFD on the future needs a live account.

**The UK's gilts and China's bonds as funds that other vendors carry** (the user's,
2026-10-18: "find alternative asset that will help track the 'china bonds' idea and will
have more voters. And do the same for gilt"). The long gilt future, and China's 10-year
future (CFFEX's T, from Sina, put in the JGB's place earlier that day), had no second free
source; their bonds' funds have vendors that agree hour by hour ("The sources' vote"):
- **China's 30-year government bond fund** (511090), the most traded bond fund there (about
  6 billion yuan a day; a median hour 0.05%). The 10-year fund (511260) moves about one
  price step a median hour (0.008%) and was not taken.
- **iShares' Core UK Gilts** (IGLT), the most traded gilt fund. Vanguard's (VGOV), chosen
  first, prints its closing auctions far off - +1.50% into the 2024-12-24 half-day close
  and back at the next open, +1.95% into 2025-04-07's: 3 of its 8 pushes in two years,
  where the FT does not vote; IGLT, at 3.5 times the volume, shows none.

Both from Yahoo's half hours on their exchange's own template (`shanghai`, `london`), so a
night runs to the first bar's close, as the indices' do. The two futures left with their
bars and code.

---

## Data and providers

**One provider per instrument, chosen by measurement**, against the stored bars hour by
hour in basis points: a feed a few bp off on a thin fund sends alerts for moves that did
not happen. A candidate answers four questions, cheapest first: does it serve the :00 bar
by :05; does its allowance cover the load; does it match the consolidated tape on thin
names; which tickers, how far back.

**A fund goes to an IEX-only feed only if IEX prices it like the tape** (median ≤ 2 bp,
p90 ≤ 5, ≤ 2% of hours missing, over 28 days): IEX is one exchange.

**Documented APIs first, undocumented endpoints last, no quota past about 85%.**

**The store is unadjusted; corporate actions are declared**: adjusted series change with
every dividend, and a ratio of adjusted to unadjusted cannot see a split (USHY's undeclared
6-for-5 read as a −103σ night).

**History from another source is written only where it agrees with the store**: returns
correlated ≥ 0.90 and a median level gap ≤ 25 bp over ≥ 200 shared hours. The level test
catches dividend-adjusted imports; only missing hours are written.

**A pair's weekend that opens exactly at Friday's close is no measurement** - a source's
stitched open, which reads the next real weekend as hundreds of σ. A future's or a fund's
zero night is real (98% of the funds' zero gaps since 2023 match the official open).

**Futures are read one contract at a time**: Yahoo's continuous series interleaves months
(corn −7.4% and back on 2026-06-25) and switches inside an hour. Each rolls where its volume
moves (`jump/futures.py`): live cattle twelve business days before its delivery month (held
to its last day it sat on 3,097 contracts against December's 18,822), stage 2's on the day
Yahoo's own series came onto the contract held now. The night across a roll is not scored,
and a bar under 5% of usual volume is a quote, so a hole.

**MarketWatch serves each market the window it allows** (2026-10-18): a Korean key asked
for ten trading days of hourly bars answers 400 ("Cannot use intraday step 'PT1H' with time
frame 'D10'") and takes eight, a Chinese one five; the rest still ten
(`marketwatch.FRAMES`). The KOSPI's vote had been an outage every time since about 10-14.

**The funds abroad's payouts are Yahoo's, IGLT's iShares'** (the user's, 2026-10-18: adjust
their nights; 2026-10-19: IGLT's from iShares itself): neither Tiingo nor Twelve Data carries
them. Each is checked on the first run after its own market opens, before its night is
scored. Yahoo dated two of IGLT's last four a day late - the record date: 2025-05-16 and
2026-05-22, where the price fell on the 15th and the 21st - and listed 2024-11's twice,
while the 30-year fund's five matched their drops. iShares' own table on the fund's page
has the ex-dates (40, to 2007), in pounds a share, so the step is taken over the store's
close before the ex-date's open: 2.04-2.26% on IGLT's four in the store, Yahoo's within
0.05 percentage points. A fund abroad's night on a listed payout, and on the trading day
before it, is still left unscored rather than adjusted (`returns.overnight_gaps`).

**The LME's metals are the LME's own, from Sina**: the right instrument with a short record
(from 2026-04-22) over a longer record of another (Shanghai tin, Kitco, COMEX aluminium).

**Gold and silver are spot from SiftingIO**, round the clock: on time (the 04:00 bar at
04:02) and 0.998–0.999 with Dukascopy's, whose archive is their history from 2003. Twelve
Data refuses silver and fills its gold's weekends.

**Every coin is its USDT pair on Binance**: one exchange, one quote currency.

**A market abroad keeps its exchange's own calendar** (`exchange_calendars`): fixed hours
fail sixteen exchanges (Tokyo's close moved on 2024-11-05, Riyadh trades Sunday to Thursday).
Where the calendar and Yahoo's bars disagree the wider session is kept: an empty hour is
skipped, a cut one lost. Their history is Yahoo's two years of hourly bars where they agree
with its folded 30-minute ones.

**A table abroad near its end asks the library daily, and the health chat weekly**
(2026-10-19): Shanghai's and Mumbai's holidays are announced a year at a time, so the library
(4.13.2) knows them only to 2026-12-31, while the NYSE table, which brought the library in,
runs short about once a year - a year learned later would have waited for the next. From 60
days out the 00:00 UTC run installs it (one install a day, not 24); from 30 days, a line each
Monday, since there is nothing to do but wait for the release. Past the end, about 20 holidays
a year would read as missing bars for the CSI 300, China's 30-year fund and the Nifty; scores
are not touched (a night's kind is measured from the bars).

**Each provider's calls are counted, not estimated** (the user's, 2026-10-12): the keys are
used only by this repository's workflows, so our count is the truth. Every run writes it by
day and purpose (`data/jump/usage/`); the limits and every share are one table
(`config/provider_limits.csv`) the caps are read from; Twelve Data's own daily count is
checked against ours. The 30 days before were added up from the runs' logs once. Twelve
Data's credits are counted from the symbols asked, not its `api-credits-used` header, the
minute's running total (2026-10-20): read as each call's cost, the vote's five calls at
00:06 were 15, and our count stood above Twelve Data's own on quiet days (10-16: 25 against
its 15) and hid the dashboard's calls on busy ones.

**A failing provider costs its instruments, not the run**: two unanswered requests in a row
stop it for the run; a missing key skips its instruments and is named; the run goes red only
when nothing at all was fetched.

**An open month is repaired by hand, inside the hourly run**: only the hourly run saves the
open months. A rolled future from the contract it held then (2026-10-19): soybeans'
2026-10-01 to 10-12 were ZSX26's last sessions, which no run asked - the history ended
09-30, stage 2's live fetch began on ZSF27 on 10-13 - and Yahoo's ZSX26 matched the store's
509 hours of it (correlation 1.0, 0.0% apart). The other holes of the four futures with
Dukascopy history - runs of two or more weekdays without a bar in the year to 10-16, Brent 18,
soybeans 13, the 30-year 12, platinum 8 - are the windows its builder dropped where no switch
stood out ("Stage 2's history is by series"); their contracts have expired and Yahoo no
longer lists them (404), so they stay.

---

## The sources' vote

**The sources vote, and the store's provider is one vote that saw it**: a majority of the
feeds that have the hour. "Any feed saw it" keeps bad prints; "all must agree" lets one
feed's bad tick take a real move out.

**A move not real is not scored, and a sent message is marked, not deleted**: out of the
yardstick it cannot inflate σ; marked, a wrong vote costs a line.

**A tie is uncertain, and stays scored** (the user's, 2026-10-08): nothing tells which side
is wrong.

**An outage is left out of the count** (the user's, 2026-10-10): counted against, one source
down turns a clear vote into a tie. When none can answer, the move is uncertain; when none
is asked (every source skips its hour), it is not voted.

**A voter failing every time for a day is named, once a day per source** (the user's,
2026-10-19). Its failures were log lines: the KOSPI's MarketWatch vote answered 400 for
about four days (2026-10-14 to 10-18) before the dashboard, measured only on pushes to dev,
showed it. A day, because a source's off hours and a provider's bad hour are not an
outage; once a day per source, with its instruments in one line, so a dead source is not a
stream. A rate limit is not counted: it is already named every run it stops a source. A
failure not seen again for three days is forgotten (`verify.failing_voters`).

**A source sees a move on its own two bars of it** (the user's, 2026-10-12): its price at the
store's previous bar and at this one moved the same way, at least half as far. On the 58's
voters (142 pairs, 2026-09-07 to 10-09) every one moves with the store in the same hour
(median 0.998; an hour off, 0.007), so no window or bridge is needed.

**A vote is taken when the move is found, then at each end of its market's session, while
the closest-reaching source still serves the hour** (the user's, 2026-10-12: "keep the
shortest reach"): 9 days wherever MarketWatch votes, 29 for the coins, 79 for the LME (its
older bars are Wallstreetcn's own), 89 for the CSI 300 - every recount inside the record's 90
days, or a vote it no longer held would be due every run. Further back the further sources
would vote alone. A provider's corrections come in through its ordinary request.

**History is not voted again** (the user's, 2026-10-08). A replay over all history was tried
and dropped: with one other source the vote cannot call a move not real, and most of history
has one at most. Do not reopen without a second source for those stretches.

**Every source on every instrument**: each that moves with it votes, but its own provider
(the user's, 2026-10-13: "take everything possible, from 1 to 99999999 voters per asset"). The source
audit tried 13 clients and 4 new fetches on every instrument from a runner with the keys,
over ~30 days. Accepted: hourly moves correlated 0.90 or more; nine in ten of the store's
big moves confirmed; no local hour where three or more were not. The same price from two
vendors is agreement, not a copy.
- **A bad hour skipped, not the source** (the user's): the FT on the Euro Stoxx 50, FTSE
  100 and SMI at their closing hour (0.08–0.19, its last bar ends before the auction);
  the Taiwan exchange and MarketWatch on the TAIEX at 13:00 (0.50, 0.46: the 13:30
  auction); Sina's CSI 300 at 11:00 (0.69, lunch); the FT's Bund at 08:00–14:00, where its
  index has no bar.
- **Dropped** (the user's): the FT on the funds (its 15:00 hour −0.47 to 0.14); Wallstreetcn's
  Nikkei (0.67, stale).
- **Left out:** MarketWatch's Bitcoin (Kraken's own pair, a second Kraken vote); Sina's COMEX
  metals and gas (a second Sina voice); the keyed providers on the coins (six free voters
  each).
- **A source with its own contract months is not asked across its own change of contract**:
  a step of 50 bp or more in the session's median offset, which a one-hour bad print
  cannot make.
- **More voters for the thin ones** (2026-10-17, the same bar, measured from here to the
  last stored bar of 10-16): EEX's own TTF month future, the month the store holds, on
  TTF (0.964 from 09-30, 5 of 5 big moves; its first voter); the FT's Sugar No. 11 (0.942
  from 09-15, 5 of 5); Tencent's half hours of the CSI 300 (0.991 from 08-17, the whole
  record, 24 of 24); Sina's Nikkei future but at 15:00 Tokyo (0.941, 24 of 26; 0.78 in that
  hour, the index's closing auction); and the funds of the Treasury futures' own bonds in
  Sina's half hours, SHY, IEF and TLT on the 2-, 10- and 30-year (0.915, 0.984, 0.977 from
  06-26; 59 of 62, 52 of 52, 45 of 45), in the funds' session only.
- **The bond funds abroad's voters** (2026-10-18, against Yahoo's half hours from 08-21):
  China's 30-year fund by Wallstreetcn's half hours (0.998, 14 of 14), Tencent's (0.985, 14
  of 14), Sina's but at 11:00, its lunch bar (0.996, 3 of 3), and MarketWatch's five days
  (0.988, 2 of 2); IGLT by the FT at 09:00-14:00 London (0.935 from 09-15, 3 of 3) - its
  opening hour and last two (0.86, 0.75, 0.17) skipped, MarketWatch's London bars under the
  bar (0.45-0.63).
- **Prices vote, not yields** (the user's, 2026-10-17): Wallstreetcn's 10-year yields moved
  with the Bund, the gilt and the Treasuries at 0.97-0.99, inverted and scaled by duration,
  and are not used.
- **Dukascopy is history only** (the user's, 2026-10-17: files, not an API): its CFDs on the
  gilt, the Bund and the 30-year moved with the store at 0.99 and served each hour within a
  minute, and are not asked.

**The keyed providers vote within a share of their allowance** (the user's, 2026-10-12),
enforced from `config/provider_limits.csv` (`usage.votes_left`): past it, the provider is an
outage for the rest of the run. Tiingo 14 an hour and 100 a day of 50 and 1,000; Twelve Data
6 a minute and 50 a day of 8 and 800; SiftingIO 300 a month of 10,000; Alpaca 50 a minute of
200.

**Alpaca's consolidated tape votes on every fund** - every exchange's trades, against the
IEX feeds' one - once it serves the move's hour whole (fifteen minutes behind).

**A first hour the feeds saw happen overnight is moved into the night**: a fund's stale first
print puts the night's move in its first hour; marked "not seen", the real move would be
gone.

**Candidates are the detector's own readings at ≥ 4σ**, on the raw yardstick and on the
detector's, so every reading it can flag is asked about.

Hourly-move correlation with the store, from the source audit (2026-10-13):

| instruments | voters | correlation |
|---|---|---|
| FX pairs | Yahoo, MarketWatch, Wallstreetcn, Tiingo, Twelve Data (not USD/CNH) | 0.91–0.999 |
| gold, silver | COMEX futures (Yahoo, MarketWatch), Sina's and Wallstreetcn's spot, Tiingo, Twelve Data (gold) | 0.985–0.999 |
| funds | Yahoo, Sina and MarketWatch but the fund's own, Alpaca's tape; UNG, SPY, REMX the keyed ones too | 0.95–1.000 |
| softs, cattle, stage 2's futures | MarketWatch's continuous series; Sina, Wallstreetcn and the FT where they carry it; SiftingIO's platinum | 0.92–1.000 |
| coins | Coinbase, Kraken, Bitstamp, Bitfinex, Alpaca, Yahoo | 0.997–1.000 |
| LME metals | Wallstreetcn; copper also COMEX's | 0.93–0.99 |
| indices abroad | MarketWatch (15), Wallstreetcn (3), the FT (5), Naver, the Taiwan exchange, Sina, Tencent, Sina's Nikkei future | 0.92–1.00 without the skipped hours |
| the Treasury futures | their bonds' funds SHY, IEF, TLT (Sina), 2026-10-17 | 0.915–0.984 |
| the Bund | the FT's S&P Euro-Bund index | 0.95 |
| TTF | EEX's own month future, 2026-10-17 | 0.964 |

Under the bar and not voters: Wallstreetcn's and Sina's grains (0.11–0.85), the FT's
Treasuries (0.89), Wallstreetcn's KOSPI (another grid), the FT's Top 40 and Nikkei. And
2026-10-17: Sina's Hang Seng future (0.81–0.88, its lunch hours left out), Wallstreetcn's
(0.67), its ASX 200 (0.86), Nikkei and sugar (too few bars) and CSI 300 (a half-hour grid);
the gilt funds IGLT (7 of 11 big moves) and VGOV (0.88), the Bund's Xetra funds (too few
trades); WisdomTree's TTF fund (0.63–0.67), Yahoo's TTF=F (0.67, most hours missing); the
FT's bars on the ASX's own id (0.36–0.65).

---

## The stored history

Settled once, and not to be reopened. **Stored bars are not rewritten**: a bad bar found
since 2026-10-09 is a row in `data/jump/verified.csv` (the user's), out of any fetch's reach.
Done before then, and kept:

- **The majors' 2012 Sunday opens are Dukascopy's**: that year's source opened every bar at
  the previous close.
- **The funds' wrong opens of 2016–2022 were mended from the tape**: 9,783 first bars where
  the two closes agree within 10 bp and the most similar funds' nights chose the tape's open.
- **From 2023 the funds' opens are left as stored** (the user's, 2026-10-09): a mend moved the
  year's pushes 209 → 206 and would not stay mended.
- **The funds' 2016–2019 price levels are left off the tape's**: a steady offset moves no
  reading but a few ex-dividend nights (REM's four).
- **14 shifted fund days are held as hours with no price**, so no fetch fills them again.
- **Two futures stretches are the next contract's**: cocoa's 2026-07-21 to 08-10, where no
  CFD switch could be located, and live cattle's September from 09-15.
- **A dividend refresh keeps the splits already declared**, or a split Tiingo does not list
  is lost.
- **Stage 2's history is by series** (the user's: Dukascopy where it reaches further):
  Dukascopy's CFDs for Brent (2011), the 30-year bond (2019), soybeans (2018) and platinum
  (2021), each switch a roll night, found where the CFD's gap to Yahoo's daily close jumps
  (a switch moves the CFD, the market moves both), a window where none stands out dropped;
  Yahoo's continuous series for the 2- and 10-year notes from 2024-10; the contract held now
  for corn, wheat and sugar from 2026-01/02. The CFDs' tick counts are scaled to each
  month's median.
- **The Bund's and gilt's history is Dukascopy's CFDs from 2018-05, less their switches**
  (the gilt's left with it, 2026-10-18):
  each quarter's change of contract is a large hour near the expiry that the 10-year yield
  does not explain (the Bundesbank's yield −0.97 with the CFD, the Bank of England's −0.94).
  Found in 29 of the Bund's 34 quarters and 25 of the gilt's, 35–201 bp; MarketWatch's
  continuous settlements, tried first, missed five of the Bund's: they rolled the same day.
- **TTF's history is EEX's own month future from 2022** (the user's, 2026-10-18: imported on
  the 140 hours the store held, not the usual 200): the month the store held each day, its
  trades folded into hours, an hour of fewer than five trades left out. Against the store's
  hours from 2026-09-29: hourly moves 0.974, 4.8 bp apart at the median. 2021's month
  futures traded 31-59 times a day and are not taken; none answered before.
- **The funds abroad's history** (2026-10-18): IGLT's two years of Yahoo's hourly bars, the
  same as its half hours (351 hours, 1.0, 0 apart), from 2024-10-21; China's 30-year fund's
  whole life, from its listing on 2023-06-13, Wallstreetcn's half hours, gated on Yahoo's
  170 hours - 29 of its five-hour sessions, not the funds' 200 hours (0.997; one opening
  off the market left out). Imported here from those free sources, not by the workflow,
  whose second run would have held only the 28 settled sessions.

---

## The repository

**A fresh repository** (the user's, 2026-10-17). The first carried about 921 MiB of history -
superseded Parquet and the development's dead ends - and the bars of 144 instruments no
longer in the basket (153 MB). This one started from the live tree in one commit, 87 MB;
the first, renamed Market-alert-old, is archived, then deleted. The bot's state, its vote
record and its usage files came across whole; the secrets were set again.

**Derived data is not tracked**: it rebuilds from the bars in under a minute.

**The libraries are held at their majors** (the user's, 2026-10-19): the bot runs unattended,
and every run installs the newest versions allowed. pandas was held below 3 already; pyarrow
(the bars' Parquet) and numpy (the metrics) floated, 26.0.0 and 2.5.4 on the day, and are
held below 27 and 3.

**A one-off tool is deleted once its work is done**: a rerun of a history builder would undo
later fixes. What they established is here.

**The open month lives on a release; a month enters git once, settled** - CSV a month,
Parquet a finished year. Hourly commits into per-instrument files cost 124 MiB a year.

**Health reaches the chat from wherever the run can still speak**: the workflow's last step
says what happens outside the monitor; a problem of one instrument goes out every run or
daily, never through the streak.

---

## Rejected

Measured or weighed, and not to be raised again.

| option | why not |
|---|---|
| time-of-day factor (Boudt, Croux & Laurent 2011) | flags it removes reverse no more often than the rest; flags it adds reverse more |
| labelling scheduled news | release-hour jumps are real; a push already lists nearby releases |
| block co-jumps, a block-residual move, a size floor | blocks are tight, so a common move shows in its members |
| a Student-t tail per class | every class has nearly the same tail against its own σ |
| per-instrument "N times a year" levels | sends the biggest crisis hours out as note rows |
| σ·√k across a hole | needs a clock of expected hours |
| a level check (30% off the two-day median) | drops real crypto crashes |
| "isolated and taken back" as a bad print | marks real moves, misses a feed sitting off for hours |
| a longer window for weekend gaps | a small gain, real shocks read quieter; one window for every instrument |
| a fund's night from Yahoo's daily official open | the feeds carry the same tape, so the opening price is no referee |
| a stale first print found from the bar alone | caught at best 7% of stale prints at 66% precision |
| holding isolated moves an hour | a third of alerts would be an hour late |
| a vote's one-hour window and 12-hour bridge | each from one case; every voter moves with the store in the same hour |
| a watchdog for the trigger's silence | the external scheduler is the party that knows a call stopped |
| Finnhub, Metal Sentinel, FXEmpire, Business Insider | quotes, not bars |
| Financial Modeling Prep, Massive (Polygon) free tiers | daily bars, or today's only after the close |
| Eulerpool, London Strategic Edge | no hourly bars for the funds |
| Kitco, Shanghai tin, COMEX aluminium, WisdomTree metal ETCs | not the LME's market |
| TradingView | ~6,300 bars without a login; its terms |
| Investing.com, the LME, CNBC, Barchart, Boursorama, CME, Xueqiu | refuse or forbid automated readers; not bypassed |
| Eastmoney, cngold | refuse requests not from China or not from their own pages; not bypassed |
| cnyes, SMM | daily bars only; prices filled in the browser |
| DailyFX, Stooq | gone; login required |
| Sina forex; Sina live cattle | six months of hourly bars; quotes without volume |
| api.binance.com | HTTP 451 from US runners; the market-data mirror is used |
| FXCM history | spliced blind; Dukascopy gates on a measured overlap |
| Interactive Brokers, paid Barchart | a funded account or about $500 a month |
| Onvista, ICE, JPX's futures quotes | Onvista's bars refused citing its terms, ICE behind Cloudflare, JPX's QUICK data not to be passed on; not bypassed |
| İş Yatırım, CNN Türk (the BIST 100) | their chart data under paths their robots.txt disallows |
| SGX, Eurex | SGX's JGB and Nikkei futures daily only (intraday 403); Eurex publishes no intraday prices |
| IG's CFDs (JGB future, gilt, Bund, LME, Asian indices) | a live account |
| a second feed of CFFEX's T (2026-10-18) | Wallstreetcn, Tencent and MarketWatch carry none; Baidu's finance API resets the connection; Hexun's futures host is gone; CFFEX's own site has a day's snapshot only; TqSdk needs an account |
| China's bond funds as voters on its 10-year future | 0.67-0.78 hour by hour (the 10-year fund's step is the future's hourly move) |
| Naver's minute charts of the world indices | empty, on a weekday too (2026-10-19): daily bars only |
| the one-voter indices' tracker funds at the FT (2026-10-19) | the ASX's STW and IOZ 0.50 (0.69 an hour shifted), the JSE's Satrix 40 0.45; nothing for Istanbul |
| US-listed country funds in their place (EWA, EZA, TUR; 2026-10-19) | three voters, but trading while the home market is mostly shut, in dollars: EWA's day against the ASX's next session 0.52, EZA's against the JSE's same day 0.72, TUR's against the BIST's same day 0.91 (two years) |
| more voters on the Hang Seng: Tencent's minutes, the FT's Tracker Fund (the user's, 2026-10-19: skip) | both pass (0.998; 0.95-0.98 at 10:00-13:59 Hong Kong), but would have changed none of its 10 far moves in two years, none a bad print by Naver's daily range |

---

## Open questions

Known and left alone on purpose.

**Dates to check** (every hourly run green since the merge of 2026-10-13); no work unless one
goes wrong:
- 10-26, the first European session after its clock change (rehearsed on the last six changes
  2026-10-19: no session slipped, no fetch skipped wrongly); 11-02 the US one;
- 10-29, TTF's first change of month in the store (below), and EEX's voter asked the next
  month from that day;
- 10-31 to 11-01, the first month end with 62 instruments: October settles from the open
  months' archive into git;
- about 11-12, IGLT's first payout checked live, from iShares (its night and the one
  before unscored); 12-04, the Bund's roll to FGBLH27 (MarketWatch serves it, 22 hourly
  bars on 10-17);
- from 11-01 the library asked each day for Shanghai's and Mumbai's 2027; their tables end
  2026-12-31, and from 12-07 the health chat says so each Monday until a release carries it.

**TTF's switch day.** MarketWatch's daily TTF is Yahoo's TTF=F to the cent, but on the last
trading day it is already the next month's (2024–26, the spread's sign as the season makes
it), and a session earlier where a holiday moves that day (2026-08-27). So the nights into
the day before, the day and the day after are unscored (`roll_ltd` 1). Open: a change inside
the session rather than at its open (10-29 shows which), and a holiday on a Monday last
trading day (next 2027-08-30). To act: mark that hour, or that Friday.

**SiftingIO's month.** We count the calendar month; its account page showed 181 used on
2026-10-06, so its month may start near the 6th. About 216 calls a weekday since the merge:
5,000–6,500 a month of 10,000, far from the 80% warning either way. To act: read the account
page about 11-06 and count its month.

**One voter only** for 10 instruments: the LME's tin, nickel and aluminium, the Bund, TTF
(EEX), the BIST 100, the ASX 200, the Hang Seng, the JSE Top 40 and the UK's gilts fund (the
FT, at 09:00-14:00 London): a move it does not see is uncertain, never not real. Tried: the
sources under the bar or refused, and other instruments in their place ("Rejected"). To act:
a second source that moves with them.

**KOSPI's and the JSE Top 40's closing auctions** (15:00–15:30 Seoul, 16:50–17:00
Johannesburg) are not in Yahoo's bars: their close is the bar before, and the auction falls
into the night. To act: a source that carries them.

**What else Wallstreetcn carries**, hourly, about 170 days back (2026-10-10): sugar, lean
hogs, the grains, soybean oil, the LME's lead and zinc, 38 indices and index futures, 52
government bond yields (`price_monitor/wallstreetcn.py`). One added would score young and
have no other voter unless another feed carries it.

**Undocumented endpoints carry most of the basket** (Yahoo, Sina, MarketWatch, Wallstreetcn,
the FT, Tencent, Naver, the Taiwan exchange, EEX, iShares' page): a changed shape raises and
the health chat names the provider, or the voter after a day of failing. To act: paid feeds.

**History no free source reaches.**

| instruments | missing before | tried |
|---|---|---|
| 2 US funds (XLK, XLE) | 2016-01 | HF Data, Twelve Data, Alpaca's tape |
| the 2- and 10-year notes | 2024-10 | Yahoo (730 days, no expired contract), Dukascopy (no note) |
| corn, wheat, sugar | 2026-01/02 | Yahoo (mixes two months around each switch), Dukascopy (no corn or wheat; its sugar is London's white) |
| live cattle | 2024-05 | Yahoo (730 days), Dukascopy |
| coffee, cocoa, cotton | 2018-01 | Dukascopy |
| LME tin, nickel, aluminium, copper | 2026-04-22 | Sina (last 1,023 bars), Wallstreetcn (about 170 days; taken), Eastmoney |
| 12 stock indices abroad | 2024-10 | Yahoo (two years of hourly bars) |
| CSI 300, BIST 100, TSX, IPC | 2026-08 | Yahoo (its older hourly bars failed the import gate) |
| TTF | 2022-01 | EEX (its month futures traded 31-59 times a day in 2021, none answered before) |
| the UK's gilts fund (IGLT) | 2024-10 | Yahoo (two years of hourly bars) |
