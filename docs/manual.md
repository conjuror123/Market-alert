# Jump manual

How the bot works and how to run it. Lines starting **Rule.** are invariants: break one
and the system is wrong rather than merely broken. Why each choice was made is in
`docs/decisions.md`.

**Contents:** 1 What it is · 2 Setup and tests · 3 The hourly pass · 4 Data in · 5 The
sources' vote · 6 Metrics · 7 The detector · 8 Delivery · 9 State and commits · 10
Running it · 11 Modules and data layout · 12 Known limitations

---

## 1. What it is

An hourly bot on GitHub Actions. It watches 173 instruments (172 in the basket plus DBC,
tracked outside it) and posts to a Telegram channel when one moves unusually **for
itself**: each move is measured against that instrument's own last half-year (the jump
test of Lee & Mykland, 2008), not against a shared percentage.

| block | instruments |
|---|---|
| equity, credit, rates | 107 US funds |
| precious, industrial metals, energy, agriculture | 26 US funds, 3 LME metals, coffee, cocoa, cotton, live cattle |
| FX | 17 pairs (16 round-the-clock, USD/BRL in its São Paulo session) |
| crypto | 16 coins |

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
| `TIINGO_API_KEY` | 27 funds |
| `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` | 30 funds live; history from 2016 |
| `SIFTING_API_KEY` | the 17 FX pairs |
| `TWELVEDATA_API_KEY` | 8 thin funds; archive and gap-fill |
| `FRED_API_KEY` | the VIX series |
| `HFDATA_API_KEY` | optional: the backfill's `deepen-etfs`, `probe-hfdata` and `fill-gaps`'s second source; since 2026-10-03 HF Data serves nothing before 2022-03 |

Yahoo, Sina, Google Finance, Binance, MarketWatch, Coinbase and Kraken need no key. A
missing key costs only that provider's instruments, and the health chat names the secret every run until it is
set. A key the provider refuses (401; 403 too, except at Twelve Data, where 403 is one
symbol beyond the plan) stops that provider for the run the same way
(`price_monitor.models.KeyRefused`).

**Rule.** Secrets never enter the repository; it is public. `config/config.yaml` may name a
secret, never hold one.

Local:

```bash
pip install -r requirements-dev.txt
pytest -q          # ~835 tests, about 2 minutes; run alone, several load large parquet files
```

The derived data (`data/jump/metrics/`, `jumps.parquet`) rebuilds from the committed
bars in under a minute. The open months of the bars are not in git: `tools/hot_bars.sh
restore` lays them down (needs `gh`, `GITHUB_REPOSITORY`, `GITHUB_REF_NAME`).

To add a provider: a client in `price_monitor/` returning `Candle` lists, its name in
`PROVIDERS` (`jump/basket.py`), a branch in `backfill.fetch_missing`, its secret in the
workflow's `env:`.

## 3. The hourly pass

Four commands, in `.github/workflows/price-monitor.yml`. **The order is load-bearing.**

```
python -m jump.backfill   fetch new bars; the other sources vote on far moves
python -m jump.pipeline   per-instrument metrics: the move r and the gap
python -m jump.jumps      score all history, words, 24-hour events -> jumps.parquet
python -m price_monitor     deliver what is due to Telegram
```

Around `backfill`, the open months of the bars are restored from and saved to a release
(`tools/hot_bars.sh`). `jumps` reads what `pipeline` wrote; delivery reads `jumps.parquet`.

**Rule.** Everything internal is UTC seconds, named `hour_utc`. Local time appears only
where a day is a local thing (a fund's session in New York), resolved through `ZoneInfo`.

**Rule.** A bar is stamped by its start. The run fires at :05 and stores the hour it is
standing in, a few minutes of it; that bar heals on the next fetch.

## 4. Data in

| provider | instruments | notes |
|---|---|---|
| Tiingo | 27 funds | IEX price matches the consolidated tape for these |
| Alpaca | 30 funds | free IEX bars live; consolidated (SIP) history from 2016 |
| Twelve Data | 8 funds (USO UNG GLD SLV CPER DBA CORN DBC) | consolidated tape for thin funds; one batched request a run |
| Sina Finance | 33 funds; LME tin, nickel, aluminium | US half-hour bars (consolidated); LME three-month contract, last 1,023 hourly bars |
| Yahoo | 34 funds; coffee, cocoa, cotton, live cattle | futures from the front contract, rolled before first notice, cattle twelve business days before its delivery month; each run asks the contract's bars from the session it became front (`jump/futures.py`) |
| Google Finance | TUR | read off the quote page |
| SiftingIO | 17 FX pairs | the bar closed at :00 is served by :05 |
| Binance | 16 coins | each coin as its USDT pair, via `data-api.binance.vision` (reachable from US runners) |
| Dukascopy, Bitstamp, Bitfinex | — | history only |
| HF Data | — | funds' history to 2020, imported; it withdrew its consolidated tape (to 2022-03) on 2026-10-03 |

Each fund's feed is chosen by measurement; see `docs/decisions.md`, "Data and providers".

**Rule.** `source` is identity, `provider` is who is asked. In `config/basket.yaml`,
`asset_id` and the file on disk are built from `source`; changing it orphans every stored
bar and verdict. `provider` changes freely.

**The store** (`jump/bars.py`), per instrument: one Parquet file per finished year, one
CSV per settled month of the current year (settled a week after the month ends), and the
open months as `YYYY-MM.open.csv` on a release, not in git. `bars.load(store, since)`
reads only the files that can hold the hours asked for.

**Rule.** A bar removed on purpose stays in the store as its hour with no price
(`bars.remove`). The gate makes it a hole; `merge` never fills it again, in any month,
whatever a fetch serves; `fill-gaps` does not see it as missing. Deleting the row instead
would let the next fetch reaching that hour put the bad bar back. Nothing writes a
whole store over: the tools that built the futures', coins' and pairs' histories were
deleted once their work was done (2026-10-08).

**Fetch** (`jump/backfill.py`): each instrument is asked from its newest stored bar less
three hours (`SETTLE_HOURS`), so a bar stored part-way through heals - or from further
back, the hour of its oldest vote due a count this run (`verify.recount_hours`, section
5), in the same request, so a bar its provider has corrected since comes in. An
instrument is skipped when its calendar says no bar can have appeared since its newest
in-session bar, no session hour since having begun (`nothing_can_have_appeared`): funds
by the NYSE table, pairs outside the Sun 17:00 → Fri 17:00 New York week, daily-session
markets outside their session. Coins
are never skipped. At most 4 never-seen instruments are seeded per run. Clients retry
three times (2 s, 4 s). A rate limit from Yahoo, Tiingo, SiftingIO or Alpaca stops that
provider for the run, as does Twelve Data's spent day; from the others a 429 fails only
that instrument. A spent Tiingo or SiftingIO budget is told every run it happens; a Yahoo
refusal is told only by what it costs, in the stale line below ("refused this run"): one
refusal costs its instruments an hour, fetched again next run. A provider that does not
answer two instruments in a row (every attempt a timeout, a failed connection or a 5xx, about 96 s each) is stopped for the run too, and
left out of the dividend check and the vote; the health chat names it
(`price_monitor.models.Unreachable`). Twelve Data's batch is retried twice, 61 s apart, on
its own thread. An instrument with no new bar for longer than its calendar allows,
answered with nothing new or refused by Yahoo run after run, is named on the health chat
on the run it passes its limit, then once a day, and only on a run its count moved, so not through the night (`stale_hours`): 3 session hours for a coin, 6 for a pair, 7 for a fund, two sessions for a
daily-session market (whose calendars, B3's aside, don't know holidays).

**Requests** (`jump/usage.py`): every answer the fetch gets, a 429 included, is counted by
provider on its HTTP session and logged once at the end of the fetch, with the quota left
where the provider's answer says it: `requests: tiingo 27 (left 4973), sifting 16 (left 4), …`.
Log only; nothing is sent. SiftingIO's "left" is a short window, not its 10,000 a month:
4 after a run's 16 requests while its account page showed 181 for the month
(2026-10-06). The month is on the account page only.

**Repair** (`backfill.repair_from_yahoo`), by hand: a fund's in-session hours missing in
the last 58 days are filled from Yahoo's 30-minute bars, folded to the hour, only if
Yahoo agrees with the store over the overlap (`verify_alignment`: correlation ≥ 0.90,
median ≤ 25 bp, ≥ 200 hours) and only where the store has no bar. For a hole in the open
month, which the hourly fetch never asks again: on 2026-10-01 and -02 Google served TUR
only its latest session (filled with 242 overlapping hours, correlation 0.9976, median
0.00 bp). Run it from the Actions tab: Price Spike Monitor → Run workflow, `repair` = the
funds, comma-separated. That run repairs first, then makes the ordinary hourly pass.

**Sessions** (`jump/sessions.py`): the NYSE table (`data/jump/sessions/nyse.csv`),
the FX week, and each daily-session market's hours (LME, ICE, CME, B3). B3's trading days
(`data/jump/sessions/b3.csv`) take its holidays out of USD/BRL's session: SiftingIO quotes
on them, flat or thin. The tables extend themselves: when under two years remain, the
hourly run appends three more years to both, leaving existing rows untouched.

**Quality gate** (`jump/quality.py`): a bar is unusable if it has no price (a removed
bar) or a price is not positive,
OHLC is inconsistent beyond half a tick, volume is negative, it duplicates an hour, it is a
future's thin bar, or it falls outside its session.

**Dividends** (`jump/corporate_actions.py`): on the first run after 09:30 New York,
Yahoo is asked for each paying fund's payouts; two funds in a row unanswered end the check
until the next run. A fund's overnight gap is scored only on a date its payouts are
confirmed through; five or more days behind, the health chat is told
once a day. The same answer carries the fund's splits, recorded as declared, so a split
day's gap is not scored whatever the ratio. The splits since 2000 were declared once
from Yahoo's list, which stocksplithistory.com matched on every split. The full refresh
from Tiingo (backfill workflow, `corporate-actions`) rewrites the payouts but keeps every
split already declared for the funds it covers, unless it declares one that day itself:
the morning check never asks back past a fund's checked-through date.

## 5. The sources' vote

A real trade shows up on other feeds; a source's bad print does not. Right after the
fetch, every reading at **4σ or more** is put to every other source that carries the
instrument, and they vote (`jump/verify.py`). The readings are the detector's own, built by
`pipeline.build_asset_metrics` and scored by `jumps` with the detector's settings
(`detector:` in `config/basket.yaml`), each asked when the detector finds it - on the raw
yardstick and on the detector's own, with the moves voted not real left out and the nights
moved (`verify.candidates`, `jumps._flag`). Every reading the detector can flag is
therefore asked about; with a bottom level under 4σ, the line follows it down.

Every source of the instrument's class is asked, except its own provider. The list is
`verify.SOURCES`, one entry per source with its measured reach (2026-10-08) and the name
the channel gives it; a source added there is asked from the next run.

| class | asked of |
|---|---|
| currency pairs and the real (17, SiftingIO) | Yahoo hourly FX (729 days back) and MarketWatch (`price_monitor/marketwatch.py`, 9 days back, every hour) |
| funds (133) | Yahoo 30-minute bars folded to the hour (59 days back), Sina 30-minute US bars (77 days back), MarketWatch hourly (`FUND/US/<exchange>/<ticker>`, 9 days back) - two of these for a fund Yahoo or Sina serves, all three for the rest - and Alpaca's consolidated tape (`Alpaca_SIP`, from 2016, fifteen minutes behind), for every fund: the 30 the store has from Alpaca's IEX feed (`Alpaca_IEX`, one exchange) too |
| coffee, cocoa, cotton (Yahoo) | Sina global futures, hourly (79 days back: coffee and cocoa from 2026-05-12, cotton from 07-20) |
| live cattle (Yahoo) | MarketWatch continuous contract, hourly (9 days back) |
| coins (16, Binance) | Coinbase's dollar pairs, hourly (`price_monitor/coinbase.py`, a year back) and Kraken's (`kraken.py`, 29 days back): a wick on Binance alone is real there and not the market's |
| LME (Sina) | not asked: the LME has no free second feed |

**Rule.** Never Tiingo, SiftingIO, Twelve Data or Google: the live run needs their
allowances, and Google is one session deep. A test pins it.

**Rule.** A source is no voter on a move whose bars it supplied to the store: its word
there is the store's (`verify.SUPPLIED`, `supplied`). Only a hand-mend inside the live
recount is listed - Yahoo, TUR's holes of 2026-10-01 and -02; who built the rest of the
history is the table in section 11, and no vote reaches it. A seam is known to the day:
the day either side is left out too.

A source that runs behind the clock (`delay`: Alpaca's tape, fifteen minutes on the free
plan) is not asked about a move until it serves the move's hour whole; it is no voter on
it until then, not an outage, and joins the next count - a fund's found-hour vote is
taken without it, its session-end votes with it. A source that needs keys (`keys`) is no
voter where they are not set.

**Two vendors printing the same bars agree, and both count.** A fund's or a future's
vendors read one exchange tape, so their bars are often identical. A source is never asked
about an instrument it provides itself: its word is the store's.

**One source's answer** (`judge`). Each feed is compared with itself, so a steady
offset is not a move.
- **Saw it:** the source moved the same way at least half as far (`REAL_SHARE`), from its
  closes up to an hour before the move to its closes up to an hour after. An hour it has no
  bar for is bridged by its nearest bars within 12 hours. Measured 2026-10-08 over 3,444
  moves: answers sit at 0.8 or more or under 0.2, half is in the gap between, and no source
  is consistently an hour late (`docs/decisions.md`).
- **Did not:** it did not move with it, or has not yet - the vote is taken with what the
  source serves at the time, and the next count looks again.
- **Outage:** the source is down, has no bar after the move yet, or is silent for 12 hours
  around it. For the softs, also a move whose span crosses Sina's own change of contract
  (`verify.switches`: a session whose median offset to the store stepped by 50 bp or
  more), where Sina's move carries the spread between two months.

**The vote** (`combine`, `judge_all`). The store's provider is one vote that saw it;
each other source is one vote, and an outage is a vote against.

| the votes | result | scored | under the move on the channel (section 8) |
|---|---|---|---|
| most saw it | real | yes | nothing |
| a tie | uncertain | yes | `⚠️ Yahoo(-2.50%), Alpaca(-2.50%), Sina(outage), MarketWatch(-0.50%)` |
| most did not | not real | no | `❌ Binance(+2.03%), Coinbase(-0.10%), Kraken(-0.10%)` |
| no other source carries it (the LME) | — | yes | nothing |

The line names the store's provider first with the stored move, then every source with
its own move over the same hours, or `(outage)`. A source with bars of its own around the
move outweighs one bridging a gap: the bridging one is an outage (USD/INR at night:
Yahoo's last bar is 10:00, MarketWatch has every hour). A rate limit, or no answer to two
requests in a row, stops a source for the run, and the health chat names it. Every source
of an instrument is asked or none that run: one not asked would count against.

**Rule.** An outage counts against the move. With most of a class's other sources down at
once, its far moves are not real - no alert - until a count after they are back, inside
the recount window.

**Rule.** With one other source, every disagreement is a tie: cattle, coffee, cocoa and
cotton. Their bad prints are uncertain and stay scored until another source is found.

**Overnight** (`overnight_move`), for a session's first hour voted not real: if most
voters saw the move from the previous session's close to that bar's close, the store
one of them, the move happened in the night. The store's first print was a stale one at
the old price (TLH 2020-03-09: gap +0.03%, first hour +3.8%; the tape opened +4.65%).
The verdict carries each source's night, its open of the hour over its last close
before. `jumps` (`with_nights`) puts the median of them in the gap, payout adjustment
kept, and the rest of the move from the previous close in the hour: the total stays the
store's. A gap left unscored stays so. Kept for good, like a move not real, and not
a doubt: the message is not marked. Only readings at 4σ or more are asked, so a stale
first print on a quiet morning stays, and so does a fund's before 2016, which no source
reaches.

**A move not real is not scored, and nothing is deleted.** `jumps` leaves its reading
out of every word and every yardstick; the bar stays in the store, and in the price path
the close check reads. A message already sent is marked, not removed (section 8).

**Voted when found, then at each session end** (`verify.due`, `sessions.last_close`,
`recount_days`). A reading is voted on the run that finds it, then again from the bars as
they then are on the first run after each end of its market's session - the NYSE close
for funds, a daily-session market's own close, 00:00 UTC for coins and pairs - until the
closest-reaching of its sources no longer serves its hour: 9 days for the funds, pairs
and cattle (MarketWatch), 29 for the coins (Kraken), 79 for the softs (Sina). After that the further sources would vote alone, and the vote stands. A source
that corrects its bars turns the vote; a bar that heals into no far move loses its vote.
The store's own provider is re-read first: on a run with a vote due, the hourly fetch
reaches back to its hour in the same request, and in an open month the fresher copy wins, for
every hour it brings back (section 4). Over 29 days Binance revised no bar (0 of 10,672
hours); over 9 days Yahoo revised 472 of 1,290 fund hours, 2 of them by more than 1 bp
(2026-10-08). Tiingo, SiftingIO, Alpaca and Twelve Data were not probed: their allowances
are for the live run.
Votes live in `data/jump/verified.csv`: not real and overnight ones are kept for good (the
detector rescores all history), the rest for 90 days, longer than any recount. A record
from before the vote reads in its words: confirmed as real, unconfirmed as not real, and
unknown - no other source had bars - counted as outages
(a tie with one source named, not real with two).

Instruments with a reading not yet voted are asked first; at most 40 requests a run, and
every source of an instrument or none. Over the 30 days to 2026-10-08, simulated hourly:
a mean of 7.7 requests a run (median 0, 95th percentile 56), in bursts at the session ends
(up to 227 at 00:05 UTC, when the coins and pairs come due together, spread over the next
runs by the cap; 37 of 460 runs over it). A request takes 0.1 to 3 s (Sina's longest).

**History is not voted again.** Older than its recount, a move's vote stands as the record
holds it, and a move never voted counts as normal. Most of history has one other source at
most (the funds' tape from 2016, the pairs' Dukascopy before Yahoo's two years), and one
source can only tie the store, never outvote it: a vote again there could only take known
bad prints back (`docs/decisions.md`).

**The history, judged once** (2026-10-07, by a tool since retired; its verdicts stay in
the record): every flagged reading with no verdict yet, asked of a source that reaches it,
by the same rule. Coins against Coinbase, pairs against Dukascopy's archive, funds against
Alpaca's tape from 2016. A stretch where the source is the store's own feed was skipped,
decided once per fund-year, which left 85% of the funds' flagged readings unasked. It added 8 not seen for the coins, 70 not seen and 5 overnight for
the pairs, and 23 not seen and 93 overnight for the funds. Live cattle (`cattle`, locally)
against every single contract Yahoo still serves (June and October 2025, the listed
ones), all at once, a contract left out where it is the store's own: 12 not seen of 20
asked, in three passes, as each pass's verdicts narrowed the yardstick and lifted
another reading (2025-08-01, Yahoo's series switching contracts overnight).

**14 fund days removed from the store** (2026-10-07), where no feed reaches: whole days
the old vendor shifted, measured as a store jump into the day and back out of it while
Yahoo's daily closes stayed flat (BWX 2007-11-05, DBA 2007-01-08, DBO 2007-08-17, EWC
2005-11-08 and 2006-10-20, EWG 2009-03-26, EWL 2005-12-13 and 2006-12-27, EWT
2005-11-25, EWU 2008-09-25, EWW 2005-09-19, EWY 2004-08-30 and 2005-08-04, LQD
2006-10-02; 94 hourly bars). They are removed bars (section 4): each day's every session
hour kept with no price, 95 in all (DBO's day had lost its last hour already), so no
fetch brings them back. The night after each is unscored.

## 6. Metrics

`jump/pipeline.py` turns usable bars into per-instrument metrics
(`data/jump/metrics/<stem>.parquet`), via `jump/returns.py`:

- **`r`, the hour's move:** close against the previous close. On a session's first bar,
  and on the bar after a missing hour, it is that bar's own open to close.
- **`gap`:** a session's first open against the previous session's last close. It is
  dividend-adjusted, and left unscored on a split, an unconfirmed dividend, a missing
  bar before the close, or a missing first hour: from a late first bar the "night" would
  span hours of trading. A pair's weekend that opens exactly at Friday's close is no
  measurement (a stitched open, or no quote) and is left unscored too.
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
| noticeable | ⬜ | 6 |
| high | 🟨 | 8.5 |
| major | 🟧 | 12 |
| extreme | 🟥 | 17 |

Each word is about three times rarer than the one below. `high` and up push
(`routing.PUSH_TIERS`); `noticeable` goes into the weekly note.

**Gaps** are scored by the same rules against earlier gaps **of their own kind** over the
same half-year: nights against nights, weekends against weekends. A gap of 48 hours or
more is a weekend. A pair has no nights: its holiday closures count as weekends. Scoring
starts at 16 nights or 7 weekends.

**Broken prints.** A reading beyond 1,000σ (`MISTAKE_SIGMA`) is not a reading and never
enters a yardstick. The hours after such a break are dropped too, until the price is back
within half of it (the returning hour included), for at most 24 hours (`STRETCH_BARS`).
Gaps have no stretch.

**When a reading is judged** (`ended`, `found_times`). An hour once it has ended. A fund's
gap with its first bar, once that bar has ended. A pair's weekend gap, and a
daily-session market's night, at the open. `found_utc` is that moment.

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
🟨 LTC/USDT · Litecoin +5.77% · 11.1×σ
📈 Rarest hour in 16 days (then 11.5×σ)
🕐 24.09.2026 02:00 UTC · next close in 17h
Nearby economic events (...)
```

The time line counts down to the close the move is checked at, then shows how much held
(`close 80%`, `next close in 72h`). Scheduled releases from 2 hours before the moved bar to
its close follow (a release after the close came after the move).

**A note row** (`noticeable`) goes into the weekly note, and a ping line points at it:

```
⬜ LTC/USDT · Litecoin +3.40% · 6.3×σ
Added to digest👆🏻👆🏻
```

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
| `data/jump/verified.csv` | second-source verdicts | every run |
| `data/jump/sessions/nyse.csv`, `b3.csv` | NYSE schedule, B3's trading days | when extended |
| `data/jump/bars/*/YYYY-MM.csv`, `YYYY.parquet` | settled bars | when a month or year settles |
| `data/jump/bars/*/YYYY-MM.open.csv` | open months | no: release `bars-live-<branch>`; the backfill reads the default branch's |
| `data/jump/vix/` | daily VIX | Saturday 04:00 UTC |
| `data/jump/metrics/`, `jumps.parquet` | derived | no: Actions cache / rebuilt |

**Rule.** Tables are written through a temp file and `os.replace` (`jump/atomic.py`),
so a killed run cannot truncate one.

A truncated `state.json` fails the run rather than read as a cold start. The commit step
runs whenever the monitor ran, failed or not, so a failed run's health streak and what it
sent are kept; it rebases and retries if the branch moved. A release that exists but can't be read fails
the run before anything is scored. The metrics cache is uploaded when changed and once a
day, so it doesn't age out.

## 10. Running it

**Trigger.** cron-job.org calls `workflow_dispatch` on `price-monitor.yml` at :05 every
hour (`POST /repos/<owner>/<repo>/actions/workflows/price-monitor.yml/dispatches` with a
PAT and the branch as `ref`). GitHub's `schedule:` is not used: it fires unreliably.

**Cost.** Fetch ~15 s, second source ~11 s, metrics ~11 s warm (~30 s cold) and events
~9 s (measured on 4 cores, 2026-10-04), whole job ~2 min, timeout 20 min. The vote's daily
recount at session ends brings it to about 8 requests a run on average, at most 40 a run
(section 5): not yet measured live.

**Quotas.**

| provider | limit | use |
|---|---|---|
| Tiingo | 50/hour, 1,000/day | 27 requests a run in session, ~245 a day; the full dividend refresh (backfill workflow, `corporate-actions`) asks all 133 funds at 45 an hour, about 3 hours, and starts only when the hourly run leaves Tiingo alone that long: from 3 h after a close to the next session's first hour |
| Alpaca | 200/min | 30 a run |
| Twelve Data | 800/day, 8/min | one batch of 8 a run, in a background thread: keep at most 8 instruments on it, as one batch is a minute's credits; a history walk (`--extend-history`, `--fill-gaps`) waits out :03–:12 past each hour and stops at 600 a day (`twelvedata.ARCHIVE_CREDIT_CAP`), so run one a day |
| SiftingIO | 10,000/month | ~8,700/month (17 pairs, skipped outside the FX week and USD/BRL's session) |
| Yahoo, Sina, Google, MarketWatch | none published | live fetch, dividend check, the vote (a few requests a run) |
| Binance | 6,000 weight/min per address | 16 a run |
| Coinbase, Kraken | per second, per address | the coins' vote: a few requests on a run after a coin moved 4σ or more, and once a day for 29 days after |

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
| session table extension | the table is not extended (under two years left) | streak, named | while it fails |
| fetch: a provider's key is missing or refused | that provider's instruments are not fetched; the others are | "X is not set" / "refused the key" | every run it happens |
| fetch: a provider fails, or Tiingo's or SiftingIO's budget is spent | its instruments keep their stored bars | "went dark" / "request budget spent" | every run it happens |
| fetch: Yahoo refuses (429) | its instruments wait for the next run | the stale line, "refused this run", once one is behind past its limit | past its limit, then daily |
| fetch: a provider does not answer twice in a row | stopped for the run, left out of the dividend check and the second source | "did not answer" message | every run it happens |
| fetch: answered, but no new bar | the instrument goes stale (`stale_hours`) | "no new bar" | past its limit, then daily |
| dividend check | a fund's payouts unconfirmed 5+ days: its overnight gaps go unscored | "Dividend check behind" | daily |
| second source | a source stopped for the run, or the pass crashed: moves judged by the other source or scored unchecked | "Second source" lines | every run it happens |
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

**Expected volume.** About 4 pushes and 11 note rows a week, about 9 messages a week, 8 of
them ringing. A quiet day is normal: green runs mean it looked and found nothing.

**Checking on it.** `PYTHONPATH=. python tools/stage_report.py` prints rates by word,
block and channel, and the biggest hours. To run the pass by hand, export the keys and
run the four commands of section 3.

**A repair.** Actions → Price Spike Monitor → Run workflow, on the live branch, with
`repair` naming the funds (`TUR`). Its log says, per fund, the hours missing, the hours
filled and the overlap it was judged on; a refused fund is left as it was (section 4).

**The mute.** `jump_alerts_muted: true` in `config/config.yaml`: the run proceeds, the
health and the calendar go out, Jump's pushes, note and pings do not.

## 11. Modules and data layout

| module | does |
|---|---|
| `jump/backfill.py` | fetch and merge, skip rules, dividend check, the second-source call; deepening and repair modes |
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
| `jump/usage.py` | the fetch's requests per provider, logged |
| `jump/audit.py` | the coverage report, `data/jump/coverage.md` |
| `price_monitor/jump_delivery.py` | messages and the week |
| `price_monitor/weekly_digest.py`, `economic_calendar.py` | the calendar |
| `price_monitor/notifier.py`, `health.py`, `__main__.py` | Telegram calls, health, the delivery entry point |
| `price_monitor/<provider>.py` | one client per provider |
| `tools/` | `stage_report.py`, `hot_bars.sh`, `run_died.sh` (the health line for a run that could not deliver) |

How far back each record reaches:

| instruments | from | built from |
|---|---|---|
| 133 US funds | 2002–2011 for 73, 2016 for 55, launch for 5 | HF Data, Twelve Data, Alpaca's tape |
| 14 FX pairs | 2003–2007; USD/CNH 2012 | Dukascopy to Twelve Data's first bar, about 2020-01 - the seven majors only to 2012, then FXCM (bars that open at the previous close) to 2020-01; the majors' 2012 Sunday opens from Dukascopy; then live |
| USD/BRL, USD/INR, USD/KRW | 2019-09, 2019-11, 2020-01 | Twelve Data |
| 16 coins | 2013–2020 by listing | Binance; earlier from Bitstamp, Bitfinex, Coinbase |
| coffee, cocoa, cotton | 2018-01 | Dukascopy CFDs, then the listed contract; cocoa's 2026-07-21 to 08-10, the roll window where no CFD switch could be located, from the December contract (rolled into on 07-21) |
| live cattle | 2024-05 | Yahoo |
| LME tin, nickel, aluminium | 2026-07 | Sina |

## 12. Known limitations

| limitation | effect | what would fix it |
|---|---|---|
| Yahoo, Sina, Google Finance and MarketWatch are undocumented endpoints | a change silences their instruments or checks until fixed; the health chat names them | paid feeds (consolidated tape, futures data) |
| no other source for the LME's metals | their bad prints are caught only beyond 1,000σ | a free independent feed |
| one other source for cattle and the softs | a disagreement is a tie: their bad prints stay scored, labelled uncertain | another free feed |
| weekend yardsticks rest on 26 weekends | ±16% noise | none chosen: a longer window gained little (`docs/decisions.md`, "Rejected") |
| history before each record's start (section 11) | "rarest since" reaches only as far as the record | paid history |
| history no source reaches, and moves under 6σ in it | judged once against sources that reach it (section 5), except: funds before 2016, the softs and the LME, and readings under 6σ. There an old bad print or stale open stays flagged (14 whole shifted fund days were removed, section 5), sits in the next half-year's yardsticks, and can be the "then" of a later "rarest since" line | a paid feed for the old history |
| repository size (~890 MiB on GitHub, 2026-10-06) | grows ~28 MB a year | a history rewrite (irreversible) |
