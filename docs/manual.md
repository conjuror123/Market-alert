# Jump manual

How the bot works and how to run it. Lines starting **Rule.** are invariants: break one
and the system is wrong rather than merely broken. Why each choice was made is in
`docs/decisions.md`.

**Contents:** 1 What it is · 2 Setup and tests · 3 The hourly pass · 4 Data in · 5 The
sources' vote · 6 Metrics · 7 The detector · 8 Delivery · 9 State and commits · 10
Running it · 11 Modules and data layout · 12 Known limitations

---

## 1. What it is

An hourly bot on GitHub Actions. It watches 62 instruments and posts to a Telegram
channel when one moves unusually **for itself**: each move is measured against that
instrument's own last half-year (the jump test of Lee & Mykland, 2008), not against a
shared percentage.

| block | instruments |
|---|---|
| equity (22) | 6 US funds; 16 stock indices abroad, each in its own exchange's hours |
| rates (7) | the 2-, 10- and 30-year Treasury futures, the Bund, the UK's gilts and China's 30-year government bonds (each as a fund: IGLT, 511090), TIP |
| credit (3) | LQD, HYG, EMB |
| energy (3) | Brent, UNG, Dutch TTF gas |
| precious metals (3) | spot gold and silver round the clock, platinum |
| industrial metals (5) | the LME's tin, nickel, aluminium and copper, REMX |
| agriculture (8) | corn, wheat, soybeans, sugar, coffee, cocoa, cotton, live cattle |
| FX (7) | 7 pairs, round the clock |
| crypto (4) | 4 coins |

`config/basket.yaml` is the source of truth for instruments, blocks and settings.

**The channel is private.** The bot is its administrator: it can edit its own messages at
any age and delete any message. Health and provider failures go to
`TELEGRAM_HEALTH_CHAT_ID`, never to the channel; without that secret they are only
logged.

It does not predict. Every number is about a move that already happened. A record cannot
be longer than an instrument's own history.

## 2. Setup and tests

Secrets (GitHub → Settings → Secrets → Actions):

| secret | for |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | the channel (required) |
| `TELEGRAM_HEALTH_CHAT_ID` | health and provider failures (optional) |
| `TIINGO_API_KEY` | 10 funds |
| `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` | the consolidated tape's vote on all 12 funds; history from 2016 |
| `SIFTING_API_KEY` | the 7 FX pairs, spot gold and silver |
| `TWELVEDATA_API_KEY` | 1 thin fund (UNG); archive and gap-fill |
| `FRED_API_KEY` | the VIX series |

Every other provider needs no key. A missing key costs only that provider's instruments,
and the health chat names the secret every run until it is set. A key the provider refuses
(401; 403 too, except at Twelve Data, where 403 is one symbol beyond the plan) stops that
provider for the run the same way (`price_monitor.models.KeyRefused`).

**Rule.** Secrets never enter the repository; it is public. `config/config.yaml` may name a
secret, never hold one.

Local:

```bash
pip install -r requirements-dev.txt
pytest -q          # ~945 tests, about 1.5 minutes; run alone, several load large parquet files
```

Each test file also passes on its own (`pytest -q tests/test_jump_backfill.py`).

**Rule.** Every run installs the newest library versions `requirements.txt` allows, so the
majors are held: pandas and numpy below 3, pyarrow below 27 (the live run's 26.0.0 and
2.5.4 on 2026-10-19). A new major is raised by hand, with the tests.

The derived data (`data/jump/metrics/`, `jumps.parquet`) rebuilds from the committed
bars in under a minute. The open months of the bars are not in git: `tools/hot_bars.sh
restore` lays them down (needs `gh`, `GITHUB_REPOSITORY`, `GITHUB_REF_NAME`; `BARS_BRANCH`
names the branch whose archive to read, as a branch without its own needs).

To add a provider: a client in `price_monitor/` returning `Candle` lists, its name in
`PROVIDERS` (`jump/basket.py`), a branch in `backfill.fetch_missing`, its secret in the
workflow's `env:`.

## 3. The hourly pass

Four commands, in `.github/workflows/price-monitor.yml`. **The order is load-bearing.**

```
python -m jump.backfill   fetch new bars; the other sources vote on far moves
python -m jump.pipeline   per-instrument metrics: the move r and the gap
python -m jump.jumps      score all history, words, 24-hour events -> jumps.parquet
python -m price_monitor   deliver what is due to Telegram
```

Before them the session tables are extended when under two years remain, or once a day
while a market abroad's is within 60 days of its end (`python -m jump.sessions
--extend-if-short`, `sessions.calendar_due`), and the open months of the bars are restored
from a release; after `jumps` they are saved back (`tools/hot_bars.sh`). `jumps` reads what
`pipeline` wrote; delivery reads `jumps.parquet`.

**Rule.** Everything internal is UTC seconds, named `hour_utc`. Local time appears only
where a day is a local thing (a fund's session in New York), resolved through `ZoneInfo`.

**Rule.** A bar is stamped by its start. The run fires at :05 and stores the hour it is
standing in, a few minutes of it; that bar heals on the next fetch.

## 4. Data in

| provider | stores | how |
|---|---|---|
| Tiingo | 10 funds | IEX prices, which match the consolidated tape for these |
| Twelve Data | UNG | the consolidated tape, for a thin fund; one request a run |
| Yahoo | REMX; 13 futures (coffee, cocoa, cotton, live cattle, Brent, the 2-, 10- and 30-year Treasuries, corn, wheat, soybeans, sugar, platinum); 16 stock indices and 2 bond funds abroad (IGLT, 511090) | the futures one contract at a time, rolled where their volume moves (`jump/futures.py`); the indices and funds abroad as 30-minute bars (15 for the Nifty) folded onto the hour |
| SiftingIO | 7 FX pairs, spot gold and silver | the bar closed at :00 served by :05; the metals from its commodities endpoint |
| Sina | the LME's tin, nickel, aluminium, copper | the three-month contract, its last 1,023 hourly bars |
| Binance | 4 coins | each as its USDT pair, via `data-api.binance.vision` (reachable from US runners) |
| MarketWatch | the Bund (one contract at a time), Dutch TTF gas (its continuous series) | about 9 trading days back, hourly, without volume |

Every other provider votes only (section 5): Alpaca's consolidated tape and coins,
Wallstreetcn, the FT, Naver, the Taiwan exchange, Tencent, EEX, Coinbase, Kraken, Bitstamp
and Bitfinex; and the providers above vote on what they carry beside their own. Some also
built history (section 11): Alpaca's tape, Wallstreetcn, EEX, Coinbase, Bitstamp,
Bitfinex, Dukascopy and HF Data, the last two for history only. iShares' page gives IGLT's payouts
("Dividends", below).

Each fund's feed is chosen by measurement; see `docs/decisions.md`, "Data and providers".

**Rule.** `source` is identity, `provider` is who is asked. In `config/basket.yaml`,
`asset_id` and the file on disk are built from `source`; changing it orphans every stored
bar and verdict. `provider` changes freely.

**The store** (`jump/bars.py`), per instrument: one Parquet file per finished year, one CSV
per settled month of the current year (settled a week after the month ends), and the open
months as `YYYY-MM.open.csv` on a release, not in git. `bars.load(store, since)` reads only
the files that can hold the hours asked for.

**Rule.** A stored price is not rewritten to fix it. A bad bar found later is a row in the
vote record (`data/jump/verified.csv`, section 5): not real, so its move is left out, or
overnight, with the night the other source saw. The bars change only by the fetch; nothing
in the code rewrites a stored price. The repairs made before this rule (2026-10-09) stay,
each with its reason in `docs/decisions.md`, "The stored history".

**Rule.** A bar removed on purpose (the 14 shifted fund days) stays as its hour with no
price (`bars.removed`): the gate makes it a hole, `merge` never fills it again and
`fill-gaps` does not see it as missing. Deleting the row would let the next fetch put the
bad bar back. Nothing writes a whole store over: each history builder is deleted once its
work is done.

**Fetch** (`jump/backfill.py`): each instrument is asked from its newest stored bar less
three hours (`SETTLE_HOURS`), so a bar stored part-way through heals - or from the hour of
its oldest vote due a count this run (`verify.recount_hours`), in the same request.
- **Skipped** when its calendar says no bar can have appeared since its newest in-session
  bar (`nothing_can_have_appeared`): funds by the NYSE table, pairs outside the Sun 17:00 →
  Fri 17:00 New York week, daily-session markets outside their session. Coins are never
  skipped. At most 4 never-seen instruments are seeded per run.
- **Failures.** Clients retry three times (2 s, 4 s). A rate limit from Yahoo, Tiingo,
  SiftingIO or Alpaca stops that provider for the run, as does Twelve Data's spent day;
  from the others a 429 fails only that instrument. A provider that does not answer two
  instruments in a row (about 96 s each) is stopped for the run and left out of the
  dividend check and the vote (`price_monitor.models.Unreachable`). Twelve Data's batch is
  retried twice, 61 s apart, on its own thread. The health chat names each.
- **Stale.** An instrument with no new bar for longer than its calendar allows is named on
  the health chat when it passes its limit, then once a day while its count moves
  (`stale_hours`): 3 session hours for a coin, 6 for a pair, 7 for a fund, two sessions for
  a daily-session market.

**Requests** (`jump/usage.py`): every answer the fetch, the vote and the VIX get, a 429
included, is counted by provider on its HTTP session and logged at the end of the fetch,
with the quota left where the answer says it: `requests: tiingo 27 (left 4973), sifting 16
(left 4), …`. SiftingIO's "left" is a few seconds' window, not its month, which is on its
account page only.

**Limits** (`config/provider_limits.csv`, `data/jump/usage/`). The limits file holds each
keyed provider's limit per window and its shares: the hourly run's; room for history walks,
gap fills, repairs and the dividend table; the dashboard's, on each code push to dev; and
the votes', enforced - past its share of an hour, a day or a month (`usage.votes_left`) a
provider is an outage in the vote for the rest of the run; its share of a minute is a pace
(`verify.per_minute`).
- Each run adds what it asked to its workflow's usage file, a row per UTC day, provider and
  purpose (fetch, votes, manual, dashboard): the hourly run `hourly.csv` (committed every
  run), the history walks `manual.csv` - two workflows committing one row would stop each
  other's push. The dashboard's calls are logged only; `dashboard.csv` holds the 30 days
  before 2026-10-13, added up from the logs once, with the others' first rows.
- Twelve Data is counted in credits, one per symbol: its batch of seven is seven. Its
  `api-credits-used` header is the minute's running total, not a call's cost; read as the
  cost until 2026-10-20, it counted the vote's five calls at 00:06 as 15.
- A day or month past 80% of a limit is said once on the health chat, and so is a day on
  which Twelve Data's own count (`daily_usage`, a credit, asked by the 23:00 UTC run) runs
  20 credits ahead of ours. The dashboard's calls are not in ours, 7 credits a run, so a
  day with three dashboard runs sets it off (2026-10-19: its 52 was our 17 and five runs' 35).
  The dashboard sets the last 30 days against each limit.

**Repair** (`backfill.repair_from_yahoo`), by hand, for a hole in the open month, which the
hourly fetch never asks again: a fund's missing in-session hours of the last 58 days are
filled from Yahoo's 30-minute bars, folded, only where Yahoo agrees with the store over the
overlap (`verify_alignment`: correlation ≥ 0.90, median ≤ 25 bp, ≥ 200 hours) and only where
the store has no bar. A rolled future of Yahoo's the same, from the contract the store held
on each missing hour's day, never the continuous series, each contract gated on the store's
own hours of it (`backfill._repair_future`); a contract Yahoo no longer lists (an expired
one, 404) leaves its hours missing. From the Actions tab: Price Spike Monitor → Run
workflow, `repair` = the funds or futures, comma-separated; the run repairs first, then
makes its ordinary pass.

**Sessions** (`jump/sessions.py`): the NYSE table (`data/jump/sessions/nyse.csv`), the FX
week, and each daily-session market's hours (LME, ICE, CME; the Bund's and TTF's 08:00-18:00
local). The 16 markets abroad (`EXCHANGES`)
each have a table in the NYSE table's format (`data/jump/sessions/<exchange code>.csv`, from
`exchange_calendars`): the days they trade and their hours that day. The tables extend
themselves: with under two years left, the hourly run appends the days through the end of
the year three years ahead. A calendar whose holidays the library knows only so far ahead
stops there (Shanghai's and Mumbai's at 2026-12-31); past a table's last row its usual week
holds, so its holidays read as missing bars. Within 60 days of such an end the 00:00 UTC run
asks the library each day, until a release carries the next year
(`sessions.short_exchange_tables`); within 30, or past it, the health chat is told on
Mondays.

**Quality gate** (`jump/quality.py`): a bar is unusable if it has no price (a removed bar)
or a price is not positive, OHLC is inconsistent beyond half a tick, volume is negative, it
duplicates an hour, it is a future's thin bar (where the future has volume), or it falls
outside its session.

**Dividends** (`jump/corporate_actions.py`): on the first run after 09:30 New York, Yahoo is
asked for each paying fund's payouts; two funds in a row unanswered end the check until the
next run. A fund's overnight gap is scored only on a date its payouts are confirmed
through; five or more days behind, the health chat is told once a day. The same answer
carries splits, recorded as declared, so a split day's gap is not scored. The full refresh
from Tiingo (backfill workflow, `corporate-actions`) rewrites the payouts but keeps every
split already declared, and the funds abroad's rows. **The funds abroad**
(`corporate_actions.FUNDS_ABROAD`: IGLT, the 30-year China fund) are in neither Tiingo
nor Twelve Data: IGLT's payouts are iShares' own (`corporate_actions.PAYOUT_SOURCES`; Yahoo
dates some of them a day late and lists one twice), each amount over the store's last
close before its ex-date's open; the China fund's are Yahoo's. Both are added with
`--add-missing` and checked on the first run after their own market opens
(`backfill.check_dividends_abroad`), in the same run before their night is scored. Their
nights are not adjusted: the night of a listed payout and the trading day's before it are
left unscored, the rest scored once confirmed.

## 5. The sources' vote

A real trade shows up on other feeds; a source's bad print does not. Right after the
fetch, every reading at **4σ or more** is put to every other source that carries the
instrument, and they vote (`jump/verify.py`). The readings are the detector's own
(`verify.candidates`: `pipeline.build_asset_metrics`, scored by `jumps` with the detector's
settings), on the raw yardstick and on the detector's - the moves voted not real left out,
the nights moved - so every reading it can flag is asked about.

**Who is asked.** Every source in `verify.SOURCES` that carries the instrument, but its own
provider: one entry per feed with what it calls each instrument, its measured reach and its
name on the channel; a source added there is asked from the next run. Each passed the
source audit's bar (`docs/decisions.md`, "Every source on every instrument").

| instruments (store) | asked of |
|---|---|
| currency pairs (7, SiftingIO) | Yahoo hourly FX (729 days back), MarketWatch (9 days back, every hour), Wallstreetcn (170 days back), Tiingo, Twelve Data (not USD/CNH) |
| spot gold and silver (SiftingIO) | Yahoo's and MarketWatch's COMEX futures (`GC=F`, `GC00`; with their own rolls), Wallstreetcn's spot (`XAUUSD.OTC`), Sina's London metals (`verify.SINA_METALS`, counted as 60 days), Tiingo, Twelve Data (gold) |
| funds (12) | Yahoo 30-minute bars folded to the hour (59 days back), Sina 30-minute US bars (77 days back), MarketWatch hourly (`FUND/US/<exchange>/<ticker>`, 9 days back), Alpaca's consolidated tape (`Alpaca_SIP`, from 2016, fifteen minutes behind) - less the fund's own provider; UNG also Wallstreetcn's natural gas future (`USNG.OTC`), Tiingo and SiftingIO, SPY SiftingIO, REMX Tiingo - each keyed one's half hours inside the regular session only |
| coffee, cocoa, cotton (Yahoo) | Sina global futures (79 days back), MarketWatch's continuous series (`marketwatch.SOFTS`), Wallstreetcn for cocoa and cotton, the FT's continuous series for coffee and cocoa: each changing contract on its own days |
| live cattle (Yahoo) | MarketWatch continuous contract, hourly (9 days back), and the FT's bars of the contract the store holds (24 trading days back) |
| stage 2's futures (Yahoo: Brent, the Treasuries, corn, wheat, soybeans, sugar, platinum) | MarketWatch's continuous series (`marketwatch.FUTURES`); the FT's bars of the contract the store holds for the grains and platinum (`ft.futures_symbol`), its continuous Brent and sugar; Sina's Brent and spot platinum (counted as 60 days); Wallstreetcn's Brent (`UKOIL.OTC`); SiftingIO's spot platinum; the Treasuries' bonds' funds SHY, IEF, TLT in Sina's US half hours (`verify.SINA_TREASURY_FUNDS`, their session only): each changing contract on its own days, or none |
| coins (4, Binance) | Coinbase, Kraken (29 days back), Bitstamp, Bitfinex, Alpaca and Yahoo dollar pairs: a wick on Binance alone is real there and not the market's |
| the UK's gilts fund (IGLT, Yahoo) | the FT's bars of the same fund (`ft.FUNDS`, by xid), but at 08:00, 15:00 and 16:00 London - its opening hour and its last two, the 16:30 auction Yahoo's: one voter, so a move it does not see is uncertain, never not real |
| China's 30-year bond fund (511090, Yahoo) | Sina's hourly bars (`sina.CN_FUNDS`, but at 11:00, the lunch break), Wallstreetcn's half hours (`wallstreetcn.FUNDS`), MarketWatch's five trading days (`marketwatch.CN_FUNDS`), Tencent's half hours (140 days) - so its recount runs 5 days |
| the Bund, TTF (MarketWatch) | the Bund the FT's S&P Euro-Bund futures index (`ft.FUTURES_CONTINUOUS`, bars only at 13:00-17:00 UTC, so not asked about the Bund's 08:00-14:00 Frankfurt; its own rolls); TTF EEX's own month future, the delivery month the store holds (`verify._eex_ttf`, 9 days back; its own rolls) |
| LME tin, nickel, aluminium, copper (Sina) | Wallstreetcn, hourly (about 170 days back, counted again for 79 days); copper also Yahoo's COMEX future (`HG=F`) |
| the stock indices abroad (16, Yahoo, `sessions.EXCHANGES`) | MarketWatch (`marketwatch.INDICES`, 15 of them, not the CSI 300; the KOSPI 8 trading days back, `marketwatch.FRAMES`), Wallstreetcn's Sensex, Saudi and Canadian (`wallstreetcn.INDICES`), the FT's Euro Stoxx 50, FTSE 100, SMI, Bovespa and IPC (`ft.INDICES`, by xid), Naver's KOSPI, the Taiwan exchange's TAIEX, Sina's CSI 300 (`sina.fetch_cn_bars`, each bar filed under the hour its last minute falls in) and Tencent's (half hours, 140 days), Sina's Nikkei future (`NK`, about 49 days, its own rolls) |

- **Skipped hours** (`Source.skip_hours`): a source whose bar in one local hour is another
  grid's is not asked about a move in that hour - neither a vote nor an outage. The FT on
  the Euro Stoxx 50, FTSE 100 and SMI at their closing hour, the Taiwan exchange and
  MarketWatch on the TAIEX at 13:00, Sina's CSI 300 and China's 30-year fund at 11:00, the
  FT on the Bund at 08:00-14:00 Frankfurt and on the UK's gilts fund at 08:00, 15:00 and
  16:00 London, Sina's Nikkei future at 15:00 Tokyo (the index's closing auction), and
  Sina's Treasury funds outside 09:00-14:00 Chicago (the funds' session). A move none of its
  sources is asked about is not voted (`verify.candidates`).
- **Keyed providers** vote within the votes' share of `config/provider_limits.csv` (section
  4); past it, each is an outage for the rest of the run (`verify.vote_room`), named on the
  health chat.
- **A source that supplied the store's bars** is no voter on them (`verify.SUPPLIED`): only a
  hand-mend inside the live recount is listed, a day either side of its seam; none now.
- **A source behind the clock** (`delay`: Alpaca's tape, fifteen minutes) is asked only once
  it serves the move's hour whole, and joins the next count. One that needs keys is no
  voter without them.

**One source's answer** (`judge`). Each feed is compared with itself, so a steady offset is
not a move.
- **Saw it:** its own price at the store's two bars moved the same way at least half as far
  (`REAL_SHARE`): its close of the store's previous bar (its open of this bar, for an hour
  measured from its open) to its close of this bar (its open, for a session's gap - not a
  stock index's, whose night runs to its first bar's close).
- **Did not:** it did not move with it, or has not yet; the next count looks again.
- **Outage:** the source is down, or has no bar or no price at either end. Also a move
  across the source's own change of contract (`verify.switches`: a session whose median
  offset to the store stepped by 50 bp or more), where its move carries the spread between
  two months.

**The vote** (`combine`, `judge_all`). The store's provider is one vote that saw it; each
other source that answers is one vote; an outage is left out of the count and named on the
line. Every source of an instrument is asked or none that run. A rate limit, or two
requests in a row unanswered, stops a source for the run, and the health chat names it. A
source that has failed every time it was asked for 24 hours (any error but a rate limit:
a 400, a page that changed) is named on the health chat once a day, with its instruments
and its last error, until it answers (`verify.failing_voters`, `voter_failures.csv`).

| the votes | result | scored | under the move on the channel (section 8) |
|---|---|---|---|
| most saw it | real | yes | nothing |
| a tie | uncertain | yes | `⚠️ Yahoo(-2.50%), Alpaca(-2.50%), Sina(outage), MarketWatch(-0.50%)` |
| most did not | not real | no | `❌ Binance(+2.03%), Coinbase(-0.10%), Kraken(-0.10%)` |
| no other source could answer | uncertain | yes | `⚠️ SiftingIO(+2.10%), Yahoo(outage), MarketWatch(outage)` |

The line names the store's provider first with the stored move, then every source with its
own move over the same two bars, or `(outage)`. With one other source every disagreement is
a tie: on the 10 instruments with one voter (the LME's tin, nickel and aluminium, the Bund,
TTF, IGLT and four indices) a bad print stays scored, uncertain.

**Overnight** (`overnight_move`), for a session's first hour voted not real: if most voters
saw the move from the previous session's close to that bar's close, the store one of them,
it happened in the night - the store's first print was a stale one at the old price (TLH
2020-03-09: gap +0.03%, first hour +3.8%). Each source on its own bars, as for a vote.
`jumps` (`with_nights`) puts the median of their nights in the gap and the rest in the hour,
so the total stays the store's. Kept for good, and the message is not marked.

**A move not real is not scored, and nothing is deleted**: `jumps` leaves its reading out of
every word and yardstick; the bar stays in the store; a message already sent is marked
(section 8).

**Voted when found, then at each session end** (`verify.due`, `sessions.last_close`,
`recount_days`): again on the first run after each end of its market's session - the NYSE
close for funds, a daily-session market's own close, 00:00 UTC for coins and pairs - while
its closest-reaching source still serves the hour (9 days wherever MarketWatch votes, 5 for
China's 30-year fund, 29 for the coins, 30 where the FT votes alone (the Bund, IGLT), 79
for the LME, 89 for the CSI 300; never past `verify.KEEP_DAYS`, 90). A
source that corrects its bars turns the vote; a bar that heals into no far move loses it.
The store's own provider is re-read first, in its ordinary request reaching back to the
voted hour; in an open month the fresher copy wins (section 4).

Votes live in `data/jump/verified.csv`: not real and overnight ones for good (the detector
rescores all history), the rest for `KEEP_DAYS`. A record from before the vote reads in its
words: confirmed as real, unconfirmed as not real, unknown counted as outages were then.

Instruments with a reading not yet voted are asked first; at most 120 requests and 300
seconds a run (`MAX_REQUESTS`, `VOTE_SECONDS`), every source of an instrument or none; those
left are counted on a later run. A request takes 0.1 to 3 s; the Taiwan exchange's is a
request a day back to the move, and Twelve Data's wait out its minute's share.

**History is not voted again** (`docs/decisions.md`): older than its recount, a vote stands
as the record holds it, and a move never voted counts as normal. The record's older verdicts
(before 2026-07-01: the coins' 12 not real, the pairs' 167 and 3 overnight, the funds' 23
and 93, live cattle's 12) were taken before the vote, mostly by one source each, and stand.

**14 fund days removed from the store** (2026-10-07): whole days the old vendor shifted, a
jump into the day and back out while Yahoo's daily closes stayed flat (BWX, DBA, DBO, EWC,
EWG, EWL, EWT, EWU, EWW, EWY, LQD, 2004–2009). They are removed bars (section 4), every
session hour kept with no price, so no fetch brings them back; the night after each is
unscored.

## 6. Metrics

`jump/pipeline.py` turns usable bars into per-instrument metrics
(`data/jump/metrics/<stem>.parquet`), via `jump/returns.py`:

- **`r`, the hour's move:** close against the previous close, as a log return. On a
  session's first bar, and on the bar after a missing hour, it is that bar's own open to
  close. Every instrument is a price: the gap and the hole are log returns too, and a
  message says each move in per cent.
- **`gap`:** a session's first open against the previous session's last close. It is
  dividend-adjusted, and left unscored on a split, an unconfirmed dividend, a missing
  bar before the close, or a missing first hour: from a late first bar the "night" would
  span hours of trading. A market abroad opening on the half hour may have its first bar at
  the next hour, as Yahoo's history files a bar under the hour it ends in. A pair's weekend
  that opens exactly at Friday's close is no measurement (a stitched open, or no quote) and
  is left unscored too. **A stock index's night runs to its first bar's close**, and that
  bar has no `r` of its own: an index's first value is computed
  from its stocks' last closes before most of them trade, so it sits at the previous
  close and the night's move comes in during the first hour (FTSE 100, the year to
  2026-10-05: the night a median 5.8 bp, the first hour 28, the other hours 11). The bond
  funds abroad keep their exchange's template, and so its night.
- **`hole`:** the move across a missing hour inside a session. It is never scored; it only
  keeps the price path whole for the close check.

A missing in-session hour is skipped, as if it were not there. Only closures the calendar
knows are gaps: a fund's nights and weekends, a pair's weekends and its Christmas and New
Year closures, a daily-session market's nights and weekends. Crypto never closes. A
future's roll night is not scored (`jump/futures.py`, `data/jump/rolls.csv`).

The pipeline extends stored metrics rather than rebuilding them.

**Rule.** The last `RECOMPUTE_TAIL_BARS` (48) rows are re-scored on every run instead of
trusted, because the stored bars heal. An instrument whose store gained bars under its
metrics (`bars_upto`) is rebuilt, and so is a fund whose payouts or splits changed
(`actions_version`, `corporate_actions.fingerprint`). A payout or split added for an old
date would otherwise never reach its gap.

**Rule.** A change to a formula moves `config_version` (`jump/versioning.py`), and the
pipeline then rebuilds cold. The first run after such a change is slow by design. Python
is hashed parsed, so its comments and docstrings don't count; `config/basket.yaml` is
hashed as bytes, so any edit to it, a comment included, rebuilds once.

## 7. The detector

`jump/jumps.py`, rescored over all history every run (about 9 s), output
`data/jump/jumps.parquet`.

**Score.** `z = r / σ`, where `σ` is the instrument's bipower volatility,
`√(π/2 · mean(|r_j|·|r_(j−1)|))`, over the 182.6 days before the hour. Bipower uses
products of neighbouring moves, so one jump cannot inflate it.

**Rule.** Rolling windows end before the bar being judged. A full-sample fit would label
a 2016 move knowing 2020.

A young series is scored once its window holds the minimum count (42 bars for a fund, 78
for a 24-hour market) and is marked `young` until the window is full.

**Words** (`LEVELS`, `config/basket.yaml` `detector.levels`), on `|z|`:

| word | square | σ |
|---|---|---|
| noticeable | ⬜ | 4.5 |
| high | 🟨 | 6.4 |
| major | 🟧 | 9 |
| extreme | 🟥 | 12.7 |

Each word is about three times rarer to reach than the one below (events at or above
each, over the whole history of the 58, measured 2026-10-11: 9,604, 3,227, 1,056, 304).
Extreme has no ceiling, so as a band of its own it is about two and a half times rarer
than major's (304 against 752). `high` and up push
(`routing.PUSH_TIERS`); `noticeable` goes into the weekly note.

**Gaps** are scored by the same rules against earlier gaps **of their own kind** over the
same half-year: nights against nights, weekends against weekends. A gap of 48 hours or
more is a weekend. A pair has no nights: its holiday closures count as weekends. Scoring
starts at 16 nights or 7 weekends. A daily-session market's gap is whole only if the bar
before it came within an hour of its session's close and the closure is no longer than
110 hours (`DAILY_CLOSED_MAX_SECONDS`), or the source was out; a market abroad's, only
if it runs from its table's trading day before, however long the closure - China's
Golden Week is a gap, a trading day without bars is not.

**Broken prints.** A reading beyond 1,000σ (`MISTAKE_SIGMA`) is not a reading and never
enters a yardstick. The hours after such a break are dropped too, until the price is back
within half of it (the returning hour included), for at most 24 hours (`STRETCH_BARS`).
Gaps have no stretch.

**The vote's word** (section 5). A move voted not real is no reading and no part of any
yardstick (`without_not_real`); a first hour voted overnight is scored with the feeds'
night in its gap and the rest in the hour (`with_nights`).

**When a reading is judged** (`ended`, `found_times`). An hour once it has ended. A fund's
gap with its first bar, once that bar has ended, and a stock index's night, which runs to
that bar's close. A pair's weekend gap, and another daily-session market's night, at the
open. `found_utc` is that moment.

**Rule.** A reading is judged only once it can be. The run at :05 sees what was found by
:00.

**Events.** An instrument's first flagged reading opens an event lasting 24 real hours
from when it was found (`event_starts`); every reading found inside belongs to it. The
event's word is its rarest reading's; its numbers are its biggest reading's.

**Rule.** An event is 24 real hours from its first move being found, for every
instrument: not a trading day, not a count of candles. A move found after them opens the
next event.

**Rarest since** (`rarest_since`). How long since the instrument last had a reading of the
same kind and direction at least 95% this size. It reads the whole stored history; "in N
years of record" means never. Spans round down: hours under 2 days, days under 2 months,
months under 2 years, then years.

**Held at the close** (`held_at_close`). Every reading, a coin's and a pair's included, is
checked at the first NYSE close after it was found (a move in the closing hour at the
next one). `held` is the share of the move still there then: 1.0 held, 1.2 kept going,
−0.2 reversed past its start.

**Detector version** (`detector_version`): a hash of the detector's parsed code
(`DETECTOR_CODE`: jumps, returns, pipeline, quality, routing, windows, sessions, futures),
`rolls.csv` and the basket.

## 8. Delivery

`price_monitor/jump_delivery.py` reads `jumps.parquet` and the verdicts, and keeps the
channel in line with them.

**A push** (`high` and up):

```
🟨 KOSPI · South Korea -5.98% · 6.5×σ
📉 Rarest hour in 2 months (then 8.2×σ)
🕐 29.07.2026 01:00 UTC · next close in 17h
Nearby economic events (...)
```

The time line counts down to the close the move is checked at (`next close in 72h`), then
shows how much held (`close 80%`); `pending` while that close's bars are late. Scheduled releases from 2 hours before the moved bar to
its close follow (a release after the close came after the move).

The name after the ticker is the instrument's `label` in `config/basket.yaml`. A stock
index abroad leads with its own name instead of Yahoo's ticker (`display`), then its place:
`Nikkei 225 · Japan`. A pair against
the US dollar is named by its other currency (`USD/JPY · Yen`, `EUR/USD · Euro`);
the move is the pair's, as its ticker reads, so USD/JPY +2% is the yen weaker.

**A note row** (`noticeable`) goes into the weekly note, and a ping line points at it:

```
⬜ EUR/USD · Euro -0.31% · 5.2×σ
Added to digest👆🏻👆🏻
```

**The note stands in blocks**, in the basket's order (equity, rates, credit, energy,
precious metals, industrial metals, agriculture, FX, crypto), each under its name-line and
left out when it has none; inside a block, time order, the rarer first within an hour. A
note longer than a message is cut between blocks: a block that does not fit what is left
of a message opens the next one whole. Only a block longer than a message is cut - the
first message's room counted after the header, which never stands alone - and the message
it goes on in opens with its name-line, `continued`:

```
━━━ 🏛 RATES ━━━

⬜ ZB=F · 30-year Treasury bond (CBOT) -0.70% · 5.2×σ
📉 Rarest hour in 18 days (then 5.0×σ)
🕐 28.09.2026 14:00 UTC · close 38%
```

A note in several messages ends each with its part and period (`part 2 of 3 - 02.10.2026
to 09.10.2026`), since only the first carries the header.

**One message per kind per run.** A run's pushes go out in one message and its pings in
one more, biggest σ first. News is said once below the pushes, named by hour when the
pushes span several hours. A message is cut at 3,000 characters (`MESSAGE_BUDGET`) so
later edits still fit. Only the run's first message rings.

**The week.** One note a week opens at the first run after the week's last NYSE close
(normally Friday 16:05 New York; `routing.digest_slot`), right after the economic
calendar's own message. It opens even when empty. The run that opens it first finishes
the old week (its checks at that close land), then opens the new note; the closing hour's
moves go into the new week.

**Rule.** One note a week, curated for that week and never after. A move belongs to the
note open when it is found. For that week, every run brings every message in line with
the events table. Anything of an earlier week is never touched, except that at the next
note the old week's ping lines are taken down.

| the event | inside its 24 hours | after them |
|---|---|---|
| new | in this run's message; rings if first | never sent |
| rarer | leaves its message (and the note), goes out in this run's message at the new word, rings | silent edit; a row turning `high` leaves the note and its ping line becomes the push |
| milder | edited (a push falling to `noticeable` shows ⬜; a `noticeable` falling away is taken out) | edited |
| same word, new numbers | edited | edited |
| gone | taken out; it can come back and ring | taken out for good |
| not real (section 5) | line kept, `❌ Binance(+2.03%), Coinbase(-0.10%), Kraken(-0.10%)` added, silently; a row leaves the note | the same |
| not real, then a reading back or a new move | the mark comes off by a silent edit; only a move rarer than the word shown before the mark rings | the same, silently |
| uncertain | scored as usual, its line under the move (`⚠️ Yahoo(-2.50%), Alpaca(-2.50%), Sina(outage), MarketWatch(-0.50%)`), in a push and in the note's row; a recount that turns the vote edits it, silently | the same |

A message is edited when what it carries changes and deleted once it carries nothing. A
ping line lives exactly as long as its row. Nothing rings more than 24 hours after its
event's first move was found (`PUSH_WINDOW_HOURS`), so a long outage cannot release old
alerts.

**The story.** A changed event carries one line of what it went through:
`✏️ ⬜ 6.2×σ 10:00 → 🟧 12.4×σ 12:00 bigger jump`. The reasons are `bigger jump`,
`arrived late`, `price corrected`, `σ corrected`, `corrected away` and `not real`. A
clean event has no story line.

**Rule.** A detector update that changes the week's events restarts the week. When
`detector_version()` changes, the updated detector's view of the week is compared with the
channel first (`_same_week`): if every event on it keeps its peak at the same word and
numbers, and no reading a previous run saw has newly become an event, only the version is
recorded and every message stays. Otherwise every push and ping message of the week is
deleted; the note and the calendar stay, and the week continues with what is found from
that run on.

**Telegram limits.** On 429 the notifier waits as told and retries up to 3 times, at most
60 s per wait and 180 s per run (`notifier.py`). A delete Telegram refuses becomes a
strike-through edit.

**The calendar** (`weekly_digest.py`): the coming week's economic releases, Monday to
Monday UTC, sent in the run that opens the note, at most 4 hours after its slot. After a
longer outage the note opens with a calendar that says so. A part Telegram refuses, and
every part after it, is kept in the state and sent first on the next runs while that week
lasts, below the note by then; the parts that went are not sent again. The archive is
topped up daily from the live feed.

## 9. State and commits

| path | what | in git |
|---|---|---|
| `data/state.json` | the week's messages on the channel, the open note | every run |
| `data/economic_calendar/` | release archive | every run |
| `data/jump/corporate_actions.csv`, `dividend_checks.csv` | payouts, how far each fund is confirmed | every run |
| `data/jump/verified.csv` | the sources' votes | every run |
| `data/jump/voter_failures.csv` | each voter failing every time it is asked, since when, when the health chat was told | when one fails or answers again |
| `data/jump/usage/hourly.csv`, `manual.csv`, `dashboard.csv` | each provider's calls a day, by purpose | every run; a history walk's; the logs' 30 days before, once |
| `data/jump/sessions/nyse.csv`, `<exchange code>.csv` | NYSE schedule, the markets abroad's | when extended |
| `data/jump/bars/*/YYYY-MM.csv`, `YYYY.parquet` | settled bars | when a month or year settles |
| `data/jump/bars/*/YYYY-MM.open.csv` | open months | no: release `bars-live-<branch>`; the backfill reads the default branch's |
| `data/jump/vix/` | daily VIX | Saturday 04:00 UTC |
| `data/jump/metrics/`, `jumps.parquet` | derived | no: Actions cache / rebuilt |

**Rule.** Tables are written through a temp file and `os.replace` (`jump/atomic.py`),
so a killed run cannot truncate one.

A truncated `state.json` fails the run rather than read as a cold start. The commit step
runs whenever the monitor ran, failed or not, so a failed run's health streak and what it
sent are kept; it rebases and retries if the branch moved. A release that exists but can't
be read fails the run before anything is scored. The metrics cache is uploaded when changed and once a
day, so it doesn't age out.

## 10. Running it

**Trigger.** cron-job.org calls `workflow_dispatch` on `price-monitor.yml` at :05 every
hour (`POST /repos/<owner>/<repo>/actions/workflows/price-monitor.yml/dispatches` with a
PAT and the branch as `ref`). GitHub's `schedule:` is not used: it fires unreliably.

**Cost.** A run takes about 2–3 minutes of its 20 (2026-10-13, the first runs on 62
instruments: the Jump step 64 s with every metric rebuilt, 42 s extended).

**Quotas.** The keyed providers' limits and shares are `config/provider_limits.csv`
(section 4).

| provider | limit | use |
|---|---|---|
| Tiingo | 50/hour, 1,000/day | 10 funds a run in session, ~90 a day; the vote within 14 an hour and 100 a day; the dividend refresh (`corporate-actions`) asks the 12 funds at 45 an hour, only from 3 h after a close to the next session's first hour |
| Twelve Data | 800/day, 8/min | UNG, one credit a run, on its own thread; a history walk (`--extend-history`, `--fill-gaps`) waits out :03–:12 past each hour and stops at 600 a day (`twelvedata.ARCHIVE_CREDIT_CAP`); the vote at most 6 a minute and 50 a day |
| SiftingIO | 10,000/month | 7 pairs and 2 metals a run, ~216 a weekday, skipped outside the FX week; the vote within 300 a month |
| Alpaca | 200/min | the tape's and the coins' vote, at most 50 a minute |
| Binance | 6,000 weight/min per address | 4 a run |
| the others | none published | the live fetch, the dividend check, the vote (at most 120 requests a run) |

**Health, in the order of the hourly run.** Everything below goes to the health chat
(`TELEGRAM_HEALTH_CHAT_ID`), never the channel. "Streak" means a failed run: the down
alert comes on the 3rd in a row (`health_alert_after_failures`) and again every 24
(`health_reminder_every_failures`), and "recovered" when they stop. One function sends all
of it (`notifier.send_health`): every quoted error is escaped and stripped of keys
(`notifier.quote`: a bot token, `apikey=` and `api_key=` values, an Authorization header), and a
message past Telegram's 4,096 characters is cut between lines, ending "…and N more lines".

| where | what goes wrong | what tells you | how often |
|---|---|---|---|
| trigger | no run starts | cron-job.org, if its call failed; the first run after a gap of over 90 minutes names the hours (`health.missed_runs`) | once, when runs resume |
| checkout, setup, the open months' restore | the run stops before delivery: nothing sent or counted | the last step, with the step's name (`tools/run_died.sh`) | 1 h after the last run that delivered, then every 24 h |
| session table extension | a table is not extended (the NYSE's under two years left, or one abroad under 60 days) | streak, named | while it fails |
| a table abroad within 30 days of its end, or past it (the library has no later year yet) | past its end its weekdays count as trading: its holidays read as missing bars | "Session table running out", naming the table and its instruments | Mondays, 00:00 UTC |
| fetch: a provider's key is missing or refused | that provider's instruments are not fetched; the others are | "X is not set" / "refused the key" | every run it happens |
| fetch: a provider fails, or Tiingo's or SiftingIO's budget is spent | its instruments keep their stored bars | "went dark" / "request budget spent" | every run it happens |
| fetch: Yahoo refuses (429) | its instruments wait for the next run | the stale line, "refused this run", once one is behind past its limit | past its limit, then daily |
| fetch: a provider does not answer twice in a row | stopped for the run, left out of the dividend check, and an outage in the vote | "did not answer" message | every run it happens |
| fetch: answered, but no new bar | the instrument goes stale (`stale_hours`) | "no new bar" | past its limit, then daily |
| dividend check | a fund's payouts unconfirmed 5+ days: its overnight gaps go unscored | "Dividend check behind" | daily |
| the vote | a source stopped for the run: an outage, left out of the count of the moves it is asked about (uncertain where none is left), until a later count; the pass crashed: the hour's moves are scored unvoted | "The vote" lines | every run it happens |
| the vote: a source failing every time it is asked, 24 hours on (an error, not a rate limit) | an outage in those votes until it answers | "… has failed every time it was asked since …", under "The vote" | once a day per source |
| VIX | both sources failed | "went dark" | every run it happens |
| pipeline or jumps, one instrument (an error, or no metrics file) | that instrument keeps its stored metrics, or its events from the last run, so delivery never reads them as gone; the others are current | "Metrics failed" / "Scoring failed for N instrument(s)", each with its error | every run it happens |
| pipeline, jumps (every instrument), or the step's 12 minutes | no new events | streak, named with the part it stopped in and its minutes | while it fails |
| open months' save (refused, too, with no open months on disk: an empty archive would replace the good one) | the next run restores an older copy and fetches the difference | streak, named | while it fails |
| delivery, calendar, digest | an exception in the monitor | streak, with the error | while it fails |
| state commit | what the run sent and counted is lost; the next run may resend | the last step (`tools/run_died.sh`) | 1 h after the last run that delivered, then every 24 h |
| Telegram itself | the token revoked or Telegram down | nothing can reach the chat; the job goes red | — |

Not a health matter: a delete Telegram refuses is struck through instead, and a detector
or basket change that changes the week's events deletes its pushes and pings once, as
expected.

The Jump steps are `continue-on-error` so delivery still runs, and the job is failed at
the end anyway when they did not complete: the fetch only when nothing at all could be
fetched, the pipeline or jumps when every instrument failed. The streak counts what fails
the run, never one instrument: a fund that went dark has its own line every run, and a run
that delivered without it is clean and green. The step
has 12 of the job's 20 minutes: past them it is stopped, and delivery, health and the
commit still run. Each part of it (fetch, pipeline, jumps) notes its start, so a step that
stops short is reported with the part it was in and its minutes.

**Expected volume.** About 5 pushes and 12 note rows a week: 259 and 635 over the year to
2026-10-19, 285 and 398 over the year before, by the dashboard's count (24-hour events, the
detector alone). The basket of 172 at 6σ made 4.0 and 10.8 a week (`docs/decisions.md`,
"Words at 4.5 / 6.4 / 9 / 12.7σ"). A quiet day is normal: green runs mean it looked and
found nothing.

**Checking on it.** The dashboard (https://conjuror123.github.io/Market-alert/) is measured
on every push of code to `dev` and by hand (`.github/workflows/measure.yml`), and drawn by
`tools/dashboard.py`; check it by loading the page, not only its `latest.json`.
- Its bars are the checkout's closed months and the live run's open months; an instrument
  the live run does not fetch yet is brought up to now by the bot's own fetch
  (`jump.backfill --live-pass`), except on Tiingo or Twelve Data, whose quotas are the live
  run's. The page names any instrument with no bar in the 4 days to the measurement.
- The voters' bars are fetched over each instrument's recount window (`tools/voter_bars.py
  --recount`). `tools/measure.py` builds the metrics cold, votes the moves the live pass
  would hold now, and scores with `jumps.run`: the detector alone over the year to the
  measurement, the year before and the archive by year, and with the votes inside their
  windows.
- Each run publishes only its own measurement; its counts are cross-added, and a check
  that fails stops it from publishing.

`PYTHONPATH=. python tools/stage_report.py` prints rates by word, block and channel, and the
biggest hours. To run the pass by hand, export the keys and run the four commands of
section 3.

**A repair.** Actions → Price Spike Monitor → Run workflow, on `main`, the live branch, with
`repair` naming the funds or Yahoo's futures (`HYG`, `ZS=F`). Its log says, per instrument,
the hours missing, the hours filled and the overlap it was judged on; a refused one is left
as it was (section 4).

**The mute.** `jump_alerts_muted: true` in `config/config.yaml`: the run proceeds, the
health and the calendar go out, Jump's pushes, note and pings do not.

## 11. Modules and data layout

| module | does |
|---|---|
| `jump/backfill.py` | fetch and merge, skip rules, dividend check, the call to the vote; deepening and repair modes |
| `jump/bars.py` | the bar store |
| `jump/sessions.py`, `futures.py` | calendars and sessions; contract rolls and the front contract |
| `jump/quality.py` | the bar gate |
| `jump/corporate_actions.py` | payouts and splits |
| `jump/verify.py` | the sources' vote |
| `jump/returns.py`, `pipeline.py` | metrics |
| `jump/jumps.py`, `routing.py` | detector; which words push, the note's slot |
| `jump/basket.py` | instruments and `detector:` settings |
| `jump/versioning.py`, `windows.py` | config hashes; the pipeline's warm lead |
| `jump/cboe.py`, `fred.py`, `vix.py`, `ewma.py`, `zscore.py` | the VIX line on the note |
| `jump/atomic.py` | safe writes |
| `jump/usage.py` | each provider's calls per run, logged and added to the usage files; the limits and their warnings |
| `jump/audit.py` | the coverage report, `data/jump/coverage.md` |
| `price_monitor/jump_delivery.py` | messages and the week |
| `price_monitor/weekly_digest.py`, `economic_calendar.py` | the calendar |
| `price_monitor/notifier.py`, `health.py`, `__main__.py` | Telegram calls, health, the delivery entry point |
| `price_monitor/<provider>.py` | one client per provider |
| `tools/` | `measure.py` and `dashboard.py` (the dashboard), `stage_report.py`, `hot_bars.sh`, `run_died.sh` (the health line for a run that could not deliver), `voter_bars.py` (every live voter's bars for a measurement; the backfill's `voter-bars` mode, where the keys are) |

How far back each record reaches:

| instruments | from | built from |
|---|---|---|
| 12 US funds | 2002–2010 for 10, 2016 for 2 | HF Data, Twelve Data, Alpaca's tape; the first bar of 9,783 mornings of 2016–2022 mended from the tape (`docs/decisions.md`, "The funds' wrong opens of 2016–2022 were mended from the tape") |
| 7 FX pairs | 2003; USD/CNH 2012 | Dukascopy to Twelve Data's first bar, about 2020-01 - the majors only to 2012, then FXCM (bars that open at the previous close) to 2020-01; the majors' 2012 Sunday opens from Dukascopy; then live |
| spot gold and silver | 2003-05 | Dukascopy's archive below SiftingIO's first bar (`deepen-dukascopy`, gated on the overlap); then SiftingIO. Dukascopy leaves the daily break (an hour from 17:00 New York) empty where SiftingIO serves a thin bar |
| 4 coins | 2013–2020 by listing | Binance; earlier from Bitstamp, Bitfinex, Coinbase |
| coffee, cocoa, cotton | 2018-01 | Dukascopy CFDs, then the listed contract (cocoa's 2026-07-21 to 08-10 the December contract's) |
| live cattle | 2024-05 | Yahoo |
| stage 2's futures | Brent 2011-01, soybeans 2018-03, the 30-year bond 2019-01, platinum 2021-11, the 2- and 10-year notes 2024-10, corn, wheat and sugar 2026-01/02 | Dukascopy's CFDs; Yahoo's continuous series for the notes; the contract held now for the grains and sugar; then the listed contract (`docs/decisions.md`, "Stage 2's history is by series") |
| 16 stock indices abroad | 2024-10; the CSI 300, BIST 100, TSX and IPC 2026-08 | Yahoo's hourly bars where they agree with its folded 30-minute ones, then those (section 4) |
| the Bund | 2018-05 | Dukascopy's CFD (BUNDTREUR), each quarter's switch dropped or a roll night in `data/jump/rolls.csv` (`docs/decisions.md`, "The stored history"); then MarketWatch's contract, from 2026-09-29 |
| TTF | 2022-01 | EEX's own month future, the month the store held each day, its trades folded into hours, hours of fewer than five trades left out (`deepen-eex`, gated on the store's hours); then MarketWatch's, from 2026-09-29 |
| the UK's gilts fund (IGLT) | 2024-10 | Yahoo's hourly bars, the same as its folded half hours (`deepen-yahoo`: 351 hours, 1.0), then those |
| China's 30-year bond fund (511090) | 2023-06, its listing | Wallstreetcn's half hours (`deepen-wallstreetcn`, gated on 170 of Yahoo's hours: 0.997, one opening off the market left out), then Yahoo's folded half hours |
| LME tin, nickel, aluminium, copper | 2026-04-22 (copper 04-23) | Wallstreetcn to Sina's first bar (2026-07-08 tin, 07-15 nickel and aluminium, 07-22 copper), less its openings off the market (section 4: 11 of tin's and aluminium's); then Sina |

## 12. Known limitations

| limitation | effect | what would fix it |
|---|---|---|
| most providers are undocumented endpoints (all but Tiingo, Twelve Data, SiftingIO, Alpaca, Binance, Coinbase, Kraken, Bitstamp, Bitfinex, Cboe and FRED) | a change silences their instruments or votes until fixed; the health chat names them | paid feeds (consolidated tape, futures data) |
| one other source for 10 instruments: the LME's tin, nickel and aluminium, the Bund, TTF, IGLT, the ASX 200, the Hang Seng, the JSE Top 40, the BIST 100 | a disagreement is a tie: their bad prints stay scored, labelled uncertain. The LME's other source, Wallstreetcn, prints the same close as Sina in 53–68% of hours (2026-07 to 10): in part one upstream | another free feed (`docs/decisions.md`, "One voter only") |
| weekend yardsticks rest on 26 weekends | ±16% noise | none chosen: a longer window gained little (`docs/decisions.md`, "Rejected") |
| history before each record's start (section 11) | "rarest since" reaches only as far as the record | paid history |
| history older than its recount | not voted (section 5): only the readings the one-off check of 2026-10-07 judged carry a vote. Elsewhere an old bad print or stale open stays flagged (14 whole shifted fund days were removed, section 5; the funds' wrong first bars of 2016–2022 were mended, section 11, but not those before 2016 or since 2023), sits in the next half-year's yardsticks, and can be the "then" of a later "rarest since" line | a second source for the old history (`docs/decisions.md`, "History is not voted again") |
| a fund's morning open from 2023 | about 1 in 100 opens 15 bp or more off the official open (1,242 mornings to 2026-10-08); the IEX feeds take it from one exchange. Only a first hour at 4σ or more is voted (section 5) | none chosen: a mend moved the year's pushes 209 → 206 and would not stay (`docs/decisions.md`, "From 2023 the funds' opens are left as stored") |
| the funds' 2016–2019 price levels | off the tape's in 680 fund-months (REM 7% high in 2017-12), stepping on payout days: REM's ex-dividend nights read as moves (four flagged) | rewriting those years from the tape (`docs/decisions.md`, "The funds' 2016–2019 price levels are left off the tape's") |
| repository size (87 MB at its fresh start, 2026-10-17) | grows about 10 MB a year from the bars (a month about 0.35 MB in git, a year's fold about 5), with the hourly state's small commits, and by its files whole on a commit that rewrites data | another fresh start, as on 2026-10-17 |
