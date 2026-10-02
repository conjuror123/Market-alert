# How Tremor works

> **Holds** how a bar becomes a message: the mechanism, the modules, the data layout.
> **Does not hold** why a choice was made (`decisions.md`), how to run it
> (`operations.md`), or the command order (`CLAUDE.md`, which is canonical).
> **Describe what runs.** If a paragraph argues for something, it belongs in
> `decisions.md`.

173 instruments — 172 in the basket plus `DBC` tracked outside it — one hourly pass, and a
message only when one of them moves unusually **for itself**.

**This branch is the jump detector, as it will run live.** It is built in stages on
`claude/youthful-pascal-u0rx7u`; production keeps running the previous detector from its own
branch until the switch, which can come now that stage 4 exists. The previous detector's code
is not here — read it on the production branch when a later stage needs a piece of it.

**The hourly pass** is four commands, and the order is load-bearing — `CLAUDE.md` lists
them and is the only copy: `backfill` fetches the bars, `pipeline` turns them into
per-instrument metrics (the move `r` and the `gap`), `jumps` scores every instrument's whole
history and writes the events, and `price_monitor` delivers what is due.

---

## The jump detector

It copies the established jump test of Lee & Mykland (2008), and adds one piece at a time.
Each stage has a named source and a measured effect; `decisions.md` records why.

**Stage 0 — built.** Two rules, on every instrument's hourly bars, and nothing else:

1. **The score.** `z = r / σ`, where `σ` is the instrument's *bipower* volatility over the
   half-year of calendar time before this hour:
   `σ = √(π/2 · mean(|r_j|·|r_(j−1)|))`. Products of neighbouring moves, so one jump in the
   window cannot inflate the yardstick. The hour being judged never enters its own `σ`.
2. **The word**, from `|z|`: `noticeable` 3.9, `high` 5.5, `major` 7.8, `extreme` 11.0 —
   each √2 bigger than the one below.

A young series is scored as soon as its window holds the paper's minimum count (42 bars for
a fund, 78 for a 24-hour market); the window then grows to half a year. Rows scored before
it is full are marked `young`.

**Stage 1b, the gap — built.** What happens while a market is shut arrives as the jump from
the last price before the close to the first after it: a fund's night and weekend, a
currency pair's weekend and its two midweek closures, Christmas and New Year's Day,
and a daily-session market's night and weekend — the LME's metals, the soft
commodities and cattle on their exchanges, the real on B3 (`sessions.DAILY_SESSIONS`; a
market with several sessions a day, as Shanghai's, has each scored, `SEGMENTED_SESSIONS`);
crypto never closes. A future's roll night is not scored:
the series jumps there by the spread between two contracts (`tremor.futures`). Each gap is scored by the same two rules
against the earlier gaps **of its own kind** over the half-year before it — a night against
nights, a weekend against weekends — so every reading of an instrument is read against the
same half-year of events. A gap spanning 48 hours or more is a weekend (a long weekend
included); a midweek holiday is a night. A currency pair has no nights: its Christmas and
New Year closures are judged with its weekends. Scoring starts at 16 nights or 7 weekends,
the paper's minimum for once-a-day and once-a-week data. The gap value is the pipeline's
`gap`: corrected for dividends, and left out on a split, an unconfirmed dividend or a
missing bar before the close. A missing first hour does not stop it: the night runs to the
first bar there is.

**A missing hour is skipped, as if it were never there.** An hour with no bar that the
calendar says should trade — a thin fund's quiet hour, a provider outage — is not a
closure. The bar after it is its own hour, open to close, scored as usual; the move across
the hole (`hole` in the metrics) is never scored, and counts only toward the price the
close check reads.

**A reading beyond 1,000σ is a broken price, not a market.** It is not a reading at all,
and it never enters a later reading's yardstick (`jumps.MISTAKE_SIGMA`).

**Stage 1, one event per 24 hours — built.** An instrument's first flagged reading, a gap
or an hour, opens an event that lasts 24 hours of real time from when it was found — not a
trading day and not a count of candles, so a fund's afternoon move and the next morning's
open are one event. Every reading found inside them belongs to it; the first found after
them opens the next. The event's word is its rarest reading's, and the numbers it shows
are its biggest reading's (`jumps.event_starts`, `jumps.events`). Over the record this
turns 28,095 settled flags into 19,185 events.

**Stage 2, channels — built.** `high`, `major` and `extreme` push: a message of their own,
at once. `noticeable` goes into the weekly note, with a small ping that points at it
(`routing.PUSH_TIERS`, `jumps.for_delivery`). There is **one note a week**, opened at the
first run after the week's last NYSE close and edited in place until the next one. The coming week's economic calendar
goes out as its own message in the same run, just before the note opens. A jump message
says only what the detector measured: the colour of the square is the word, and the size is
`|move| / σ` over the last half-year — the hour's σ, or the night's or the weekend's for a
gap — at the end of the first line:

```
🟥 LTC-USD · Litecoin +5.76% · 11.0×σ
📈 Rarest hour in 7 months (then 10.7×σ)
🕐 24.09.2026 02:00 UTC
```

and a note row's ping:

```
⬜ LTC-USD · Litecoin +2.89% · 5.3×σ
Added to digest👆🏻👆🏻
```

No block/own split: that is stage 8, and comes back with it.

**Stage 3, rarest since — built.** The second line of a push or a note row (not the ping)
says how long since the instrument was last at least this rare: the most recent earlier
reading **of the same kind** — hours against hours, nights against nights, weekends against
weekends, each in its own σ — **in the same direction**, at least 95% of this one's size or
bigger (`jumps.rarest_since`, `RARE_SHARE`). It reads the whole stored history, and names
that reading's size:

```
📈 Rarest hour in 6 months (then 7.1×σ)
📉 Rarest night in 3 years (then 6.1×σ)
📉 Rarest weekend in 6 years of record
```

The last form is a record: nothing at least as rare since the instrument's first bar. The
span is rounded down — hours under two days, days under two months, months under two
years, then years — so "in 2 years" holds for 2.6 of them. Over the last year the median
line reads 11 days for a `noticeable` hour, 49 for a `high` one and 258 for a `major`;
nights and weekends reach back about a year or more, being one a day and one a week.

**Stage 4, held at the close — built.** Every flagged reading — a coin's and a currency
pair's too — is checked at the first NYSE close after it was found; a move in the closing
hour is checked at the next one (`jumps.held_at_close`, `routing.next_close`). `held` is the
share of the move still there, from the price before it to the last bar ending at the close,
once the bars reach it. The message says it on its time line — counting down first, then the
answer:

```
🕐 24.09.2026 14:00 UTC · close in 5h
🕐 24.09.2026 14:00 UTC · close 80%
🕐 25.09.2026 19:00 UTC · next close in 72h
```

"Next close" when the close is on a later New York day than the move was found. Over the
record, `high` hours are still at least half there at the close 69–78% of the time (median
93–100%), fund gaps 76–83%, and an FX weekend gap is half gone by Monday's close.

The output is `data/tremor/jumps.parquet`, every instrument's events — hour, night or
weekend — each with `found_utc` and its 24-hour event's `event_start`, and the columns
the delivery layer reads (`reading_id`, `tier`, `channel`, `sigma_lt`, `since_utc`,
`since_z`, `record_start`, `check_utc`, `held`). It is rescored from the whole history every run, in about three
seconds, so nothing of it is cached. `tools/stage_report.py` prints what the detector flags:
per week, per instrument and block, the gaps by kind, how the biggest hours of each record
were worded, the events by channel, and how far back the rarest-since line reaches.

**All the stages**, in the order they are built; each one after the previous has been
reviewed:

| | stage | source | state |
|---|---|---|---|
| 0 | the score and the word: half-year bipower σ, 3.9 / 5.5 / 7.8 / 11.0 | Lee & Mykland (2008) | built |
| 1b | the gap: nights and weekends, each against its own kind (was stage 6) | Lee & Mykland (2008) | built |
| 1 | one event per 24 hours | — | built |
| 2 | channels, the weekly note, delivery and curation | — | built |
| 3 | rarest since | — | built |
| 4 | held at the funds' close | — | built |
| 5 | time of day and weekday | Boudt, Croux & Laurent (2011) | to come |
| 7 | block co-jumps | Bollerslev, Law & Tauchen (2008) | to come |
| 8 | the own move, after the block | Bollerslev, Law & Tauchen (2008) | to come |
| 9 | scheduled news, labelled rather than hidden | Lahaye, Laurent & Neely (2011) | to come |
| 10 | tail shape per asset class (optional) | Student-t, from risk management | to come |
| 11 | a size floor, only if stage 8 needs one | — | to come |
| 12 | tuning the threshold and the step | — | to come |

Stages 1–4 were what the switch to production waited on. The blocks enter the detector
only at stages 7 and 8; until then each instrument is judged on its own history alone.

Settings live under `detector:` in `config/basket.yaml`: `window_days`,
`noticeable_sigma`, `step`.

---

## Delivery

**When a move is found.** `jumps` judges a reading only once it can be (`jumps.ended`): an
hour once it has ended, a fund's gap with its first bar once that bar has ended, a currency
pair's weekend gap and a metal's night at their open. That moment is the event's `found_utc`, and the run five
minutes later is the one that sees it. `jumps.parquet` holds every flagged reading;
delivery groups them into 24-hour events itself, holding the events already on the channel
to the 24 hours they started with (`jumps.event_starts` with anchors) — a first move
corrected away does not slide its event later, and a bar that arrives late just before an
event on the channel joins it.

**A push** is its own message: the first line and the hour, and beneath them the scheduled
releases in the hours around the move (`Nearby economic events`) when there are any.
**A note row** goes into the weekly note, whose header carries the VIX line (the fear
gauge, `tremor.vix`); since Telegram does not notify on an edit, each row gets a small ping
pointing up at the note.

**The week** (`tremor_delivery`). The note opens at the first run after the week's last NYSE
close, just after the economic calendar's own message, and a move belongs to the note open
when it is found — the hour checked in the opening run, the closing hour, goes into the new
note. That run first finishes the old week, so its moves' checks at that close land on it,
then closes it, then opens the new note, and only then sends what it found. For that week every run re-reads the
events table and brings every message of the week in line with it; what belongs to an
earlier note is history and is never touched. A move found after the note opened starts a
new event, even inside the 24 hours of one from the week before. Per event:

| the event | inside its 24 hours | after them |
|---|---|---|
| new | `high` and up: a push, rings. `noticeable`: a row and its ping, rings | never sent |
| rarer — any cause but a detector update | its message is deleted (the row and its ping, or the push) and it goes out again at the new word, and rings — a ⬜ push that turns `high` again included | silent: a push is edited; a row turning `high` leaves the note and its ping is edited into the push |
| milder | edited: a push falls a colour, to ⬜ at `noticeable` | the same |
| same word, other numbers (a bigger hour, a fix) | edited | edited |
| gone | deleted — the push, or the row and its ping; it can come back and ring | deleted, for good |

**The story.** A changed event says what it went through on one line under its time, in
the push or the note row (not the ping); a clean event says nothing:

```
✏️ ⬜ 4.0×σ 10:00 → 🟧 7.9×σ 12:00 bigger jump
✏️ 🟨 6.0×σ 10:00 → ⬜ 4.6×σ 10:00 price corrected
✏️ 🟨 6.0×σ 10:00 → ✖ corrected away → 🟨 6.0×σ 10:00 price corrected
```

Each state is its colour, size and hour, and why it moved there, in the jump detector's
terms — what can move |move| / σ of an event's biggest reading: `bigger jump` (a new hour
went further), `arrived late` (a bar or gap that was missing came in), `price corrected`
(the provider revised the bar), `σ corrected` (older bars were revised, so the half-year
yardstick moved), `corrected away` (no longer a jump).

When the next note opens, the week's pings are deleted and the rest stays as it is. A part
the note no longer needs is deleted. The bot is an administrator of a public channel; a
delete it is refused anyway is struck through by an edit.

**A detector update** — a new `jumps.detector_version()`, the hash of the detector's parsed
code and the basket — restarts the week at that run: every push and ping of the week is
deleted, the note and the calendar stay, and the note shows only what is found from then
on. The first run of this delivery on the previous one's state is handled the same way.

---

## Where the bars come from

Chosen per instrument by measurement: each candidate was compared against the stored bars
hour by hour in basis points, against a yardstick of 20–40 bps for one sigma of an hourly
move.

| provider | names | role |
|---|---|---|
| Tiingo | 27 | funds whose single-exchange (IEX) price matches the consolidated tape |
| Alpaca | 30 | more IEX-safe funds, from Alpaca's free IEX bars, fresh at :05; also SIP history from 2016 |
| Twelve Data | 8 | consolidated tape for thin funds IEX misprices, one or two per commodity block: USO UNG DBC GLD SLV CPER DBA CORN; also archive and gap-fill |
| SiftingIO | 17 | the FX pairs: 0.11–0.35 bps median against the stored bars, the bar closed at :00 served by :05; USD/BRL only in its São Paulo session |
| Sina Finance | 33 | half of the remaining thin funds: its half-hour US bars are the consolidated tape (0.0 bp against Alpaca's SIP on all 67 over 28 days, 100% of its volume, no hour missing) |
| Yahoo | 34 | the other half, consolidated; also the morning dividend check |
| Yahoo futures | 4 | coffee, cocoa, cotton, live cattle: the front contract itself, rolled before first notice — `tremor/futures.py`; coffee's, cocoa's and cotton's history from Dukascopy's CFDs (`tools/dukascopy_futures.py`) |
| Sina Finance (LME) | 3 | tin, nickel, aluminium: the LME's three-month contract, traded hourly bars with volume; the last 1,023 only (`price_monitor/sina.py`) |
| Google Finance | 1 | TUR: the one feed whose hourly closes match the tape on it, read off the quote page (`price_monitor/google.py`) |
| Binance | 16 | crypto: every coin as its USDT pair on one exchange, from Binance's market-data mirror (`data-api.binance.vision`), which US runners can reach (`price_monitor/binance.py`) |
| Dukascopy, HF Data | — | history below what the live providers reach |

Which feed each fund is on, and why — the IEX line, the order of preference, the quota
headroom — is in `decisions.md` ("The data").

`source` in `config/basket.yaml` names the store — `asset_id` and the file on disk are
built from it, so it never changes when the fetch moves. `provider` is who is asked, and
changes freely.

---

## The modules

**Data in**
`bars` (the store: a shard per year before 2026 and per month since; the open months as CSV on a release, `tools/hot_bars.sh`, settled ones as `.csv.gz` in git) · `backfill`
(fetch and merge, session-aware skipping, the morning dividend check, the deepening and
repair modes) · `sessions` (NYSE calendar, the FX reference week, and the futures', metals'
and B3's own sessions) · `futures` (contract rolls, the front contract, thin bars, the
continuous history's one cleaning) · `corporate_actions` (ex-dates and splits) · `cboe` + `fred` + `vix` (the daily VIX
series and the fear-gauge line) · `quality` (bar quality gate) · `audit` (coverage table) ·
`atomic` (write through a temp file, so a killed run cannot truncate a table in place)

**Per instrument**
`returns` (the move and the gap) · `pipeline` (assembles them, extending stored metrics
rather than rebuilding them, and re-scoring the last two days in case a bar has been
completed or corrected since; an instrument whose store gained bars under its metrics —
each row keeps how many it was computed from, `bars_upto` — is rebuilt)

**The jump detector**
`jumps` (the half-year bipower score, the words, the gaps by kind, 24-hour events, the
delivery columns) · `routing` (which words push, and the weekly note's slots) · `basket`
(the instruments and the `detector:` settings)

**Bookkeeping**
`versioning` (config and run hashes; the code, parsed, not the bytes) · `windows` (the
pipeline's warm lead and the VIX line's settings) · `ewma` + `zscore` (the VIX line's
long-run sigma and short-memory state)

**Delivery** lives in `price_monitor/`: `tremor_delivery` (renders the messages and
curates the week's channel; the word and the channel are already stamped), `weekly_digest` (the economic calendar, sent just before the weekly
note opens), `health`, `notifier`, and the source clients `backfill` fetches through: live, `tiingo`, `alpaca`, `sifting`,
`twelvedata`, `sina`, `yahoo`, `google`, `binance`; history only, `dukascopy`,
`hfdata`, `bitstamp`, `bitfinex`. One-off history builders are in `tools/`:
`futures_history`, `binance_history`; `sina_history`, `bitstamp_fill` and the `kitco`
and `coinbase` clients built records no longer in the basket.

Product pushes go to `TELEGRAM_CHAT_ID`. Health and named provider failures go to
`TELEGRAM_HEALTH_CHAT_ID`, and without it only to the log — never the public channel.

---

## Where the data lives

```
data/tremor/bars/*/YYYY.parquet     hourly bars, a year a shard, before 2026            TRACKED
data/tremor/bars/*/YYYY-MM.csv.gz   hourly bars, a settled month a shard                TRACKED
data/tremor/bars/*/YYYY-MM.csv      the open months             release bars-live-<branch>
data/tremor/vix/                   daily VIX close                                   TRACKED
data/tremor/corporate_actions.csv  ex-dates and splits                               TRACKED
data/tremor/dividend_checks.csv    how far each fund's dividends are confirmed       TRACKED
data/tremor/sessions/              the NYSE schedule                                 TRACKED
data/state.json                    what has been sent, and the open note             TRACKED
data/tremor/metrics/               per-instrument metrics: the move and the gap gitignored
data/tremor/jumps.parquet          routed events — what delivery reads          gitignored
config/basket.yaml                 the instruments, blocks and the detector settings
config/config.yaml                 the mute and the health thresholds
```

Only the bars are committed, and of them only settled months: the open ones are kept
between runs on a release, which every run restores before the backfill and saves after it.
Everything computed from them is gitignored; the hourly job
keeps the metrics in the Actions cache and extends them, and rebuilds an instrument from
its bars when that cache is missing, `config_version` has moved, or bars were written under
its metrics (a deepening, a filled hole) — about ten seconds for all of them. The events
are rescored whole every run. The bar archive is the only thing here that cannot be rebuilt. How far back each record
reaches (2026-10-02):

| instruments | from | built from |
|---|---|---|
| 133 US funds | 2002–2011 for 71, 2016 for 55, launch for 7 (FALN, GIGB, IGIB, USIG, USHY, XLC, JMBS) | HF Data (2002 on), Twelve Data (2020-02 on), Alpaca's consolidated tape (2016 on) |
| 14 currency pairs | 2003–2007; USD/CNH 2012 | Dukascopy, then the live feed |
| USD/BRL, USD/INR, USD/KRW | 2019-09, 2019-11, 2020-01 | Twelve Data |
| 16 coins | each pair's Binance listing — LINK 2019-01, POL 2019-04 (as MATIC), ATOM 2019-04, DOGE 2019-07, SOL and DOT 2020-08, UNI and AVAX 2020-09, FIL and AAVE 2020-10, ADA 2018-04 — and under Binance's first months a dollar exchange's: BTC 2013-02, LTC 2013-12, ETH 2016-04, XRP 2017-03, BCH 2018-01 | Binance; Bitstamp, Bitfinex, Coinbase below 2018-06 (`tools/binance_history.py`) |
| tin, nickel, aluminium (LME) | 2026-07 | Sina |
| coffee, cocoa, cotton | 2019-01 | Dukascopy's CFDs to the seam (coffee 2026-08-14, cocoa 2026-08-11, cotton 2026-06-17), then the listed contract |
| live cattle | 2024-05 | Yahoo's continuous series, cleaned |

What is missing below these, and what was tried for it, is in `concerns-for-later.md`.
