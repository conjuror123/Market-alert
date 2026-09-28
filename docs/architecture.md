# How Tremor works

> **Holds** how a bar becomes a message: the mechanism, the modules, the data layout.
> **Does not hold** why a choice was made (`decisions.md`), how to run it
> (`operations.md`), or the command order (`CLAUDE.md`, which is canonical).
> **Describe what runs.** If a paragraph argues for something, it belongs in
> `decisions.md`.

61 instruments — 60 in the basket plus `DBC` tracked outside it — one hourly pass, and a
message only when one of them moves unusually **for itself**.

**Two detectors live in this repository for now.** The one that sends messages today (the
*running detector*, below) and its replacement, a **jump detector** built in stages on the
branch `claude/youthful-pascal-u0rx7u`. The jump detector replaces the running one once it
can send messages (stages 1–4); until then production runs the old one, unchanged.

---

## The jump detector (being built)

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
currency pair's weekend (crypto never closes). Each gap is scored by the same two rules
against the earlier gaps **of its own kind** over the half-year before it — a night against
nights, a weekend against weekends — so every reading of an instrument is read against the
same half-year of events. A gap spanning 48 hours or more is a weekend (a long weekend
included); a midweek holiday is a night. Scoring starts at 16 nights or 7 weekends, the
paper's minimum for once-a-day and once-a-week data. The gap value is the pipeline's
`gap`: corrected for dividends, and left out on a split, an unconfirmed dividend or a
missing bar before the close.

**Stage 1, one event per instrument per day — built.** The first flagged reading of an
instrument's day, its gap or an hour, opens the day. A later reading that day is kept only
if it reaches a *rarer* word than anything kept before it: a day that starts `noticeable`
and turns `high` says so, while a second `noticeable`, or a `high` after a `major`, is
dropped. A day therefore holds at most four events, each rarer than the last. A gap goes
before the hour that shares its timestamp. The day is the fund's New York date and the UTC
date for currency pairs and coins (`sessions.day_tz`, the running detector's day).

The output is a table, not messages: `data/tremor/jumps.parquet`, every instrument's events
— hour, night or weekend — with `escalation` marking the ones that raised their day. `tools/stage_report.py` prints what it flags:
per week, per instrument and block, the gaps by kind, and how the biggest hours of each
record were worded.

**Stages to come**, each only after the previous one has been reviewed:

| | stage | source |
|---|---|---|
| 2 | channels (push or note) and delivery to Telegram | — |
| 3 | the "biggest since …" date | — |
| 4 | the held-at-next-close check | — |
| 5 | time of day and weekday | Boudt, Croux & Laurent (2011) |
| 7 | block co-jumps | Bollerslev, Law & Tauchen (2008) |
| 8 | the own move, after the block | Bollerslev, Law & Tauchen (2008) |
| 9 | scheduled news, labelled rather than hidden | Lahaye, Laurent & Neely (2011) |
| 10 | tail shape per asset class (optional) | Student-t, from risk management |
| 11 | a size floor, only if stage 8 needs one | — |
| 12 | tuning the threshold and the step | — |

Settings live under `detector:` in `config/basket.yaml`: `window_days`,
`noticeable_sigma`, `step`.

---

## The running detector (production until the switch)

A 1.5% hour is nothing in Solana and enormous in short Treasuries, so a single percentage
threshold across a basket says almost nothing. Every instrument is measured against **its
own history**, and the message carries a date — *the biggest move since 3 March 2020*.
Before asking how unusual a move is, it subtracts what the instrument's block did, so that
"gold moved" and "everything moved, gold included" are different messages.

**The hourly pass** is four commands, and the order is load-bearing — `CLAUDE.md` lists
them and is the only copy. `saed` reads what `pipeline` wrote and builds its cross-section
in memory, which is why `cross_section` is a module and not a step.

**1. The return, and the gap before it.** Each hourly bar is scored on the move inside it;
the first bar of a session is measured from its own open. What happens while a market is
shut — a fund from 16:00 to 09:30, a currency pair over the weekend — arrives as the
**gap**, a separate reading: the jump from the last price before the close to the first
after it. It is compared with the instrument's own usual gap after the same kind of close
(weeknight or weekend; a midweek holiday counts as a weeknight), goes through the same
checks and rungs as an hour, and makes its own events. A gap is left unscored when it
might not be the market: an unconfirmed dividend, a split, a missing bar before the close.
Crypto never closes, so it has no gap. (`tremor/gaps.py`, always computed over the full
history.)

**2. Separate the block's move.** Each hour, the system asks what the rest of the block did
(`F`, the typical move of the other members) and how strongly this instrument usually
follows it (`beta`), and splits the move in two: `r = beta·F + e`. `beta` is re-learned
every hour from the previous 500 hours, which stop 3 hours before the hour being judged,
so the move being judged can never teach the system what normal is. `beta·F` is the
block's share; `e` is the instrument's own move.

**3. Standardise across the hour.** The own move is divided by its short-memory usual
size, by the usual size of that hour of day for a US fund (the opening is judged against
openings), and then by the spread of *the other instruments'* standardised own moves that
same hour — the BMP statistic, leave-one-out. With few peers this is a t rather than a z,
and a normalising transform maps it to z.

**4. Rank it.** Every hour is checked two ways: how big the *raw move* `r` is against the
instrument's usual hourly move (**absolute**), and how big its *own move* `e` is after step
3 (**abnormal**). Either is enough. Each check has four rungs, `noticeable`, `high`,
`major`, `extreme`, set per block from three tables in `tremor/severity.py`
(`BLOCK_SIGMA`, `BLOCK_RESID_SIGMA`, and `BLOCK_MOVE_SIGMA` for a block's own series). An
hour clearing both is reported at the rarer rung and marked `both`. Separately, the raw
move must be at least twice the instrument's usual hourly move (`min_move_sigma`), so a
statistically odd but tiny move is never sent. The date in the message is the last time
this instrument moved at least this much. Blocks are ranked the same way on their own
series — the median move of their members — so a whole sector moving together is an event
of its own; a block's bottom rung isn't delivered.

**5. One event per instrument per trading day.** End of session for the funds, end of the
UTC day for crypto. A later, bigger move the same day updates the event rather than opening
a second; when the gap and an hour both fire, the rarer describes the day
(`saed.merge_days`).

**6. Route it.** `major` and `extreme` interrupt at once. `noticeable` and `high` go into
the running digest note. Retention decides what the sent message says, not whether it is
sent.

**7. Deliver.** A push goes out the hour it is found and is final when it arrives. A digest
row goes into the note for its period — opened Monday and Saturday at 00:05 UTC and edited
in place — with a throwaway ping, since Telegram does not notify on an edit; a ping exists
only while the note beneath it shows its row. Messages are then corrected as the market
answers: at this day's close and the next day's close. Nothing older than 48 hours is sent.

Each alert splits the move into the block's share and the instrument's own, adding back to
it exactly, and gives the size against the instrument's usual hour and the date it was last
this big.

**Warm runs.** The "biggest since" lookup is saved between runs in the record book, as of a
checkpoint 14 days back, so an hourly run reads only its warm-up plus the bars after the
checkpoint — about a third of the bars — and publishes the events after it.

---

## Where the bars come from

Chosen per instrument by measurement: each candidate was compared against the stored bars
hour by hour in basis points, against a yardstick of 20–40 bps for one sigma of an hourly
move.

| provider | names | role |
|---|---|---|
| Tiingo | 37 | 8 FX pairs + the funds whose single-exchange price matches the consolidated tape |
| Yahoo | 15 | the thin funds where one exchange is *not* the same price; also the morning dividend check |
| Coinbase | 9 | crypto |
| Twelve Data | — | archive, gap-fill and deepening; not on the hourly path |
| Dukascopy, HF Data | — | history below what the live providers reach |

The liquid funds agree with the stored bars to under a basis point. The thin
single-commodity funds do not — on one exchange's prints they drift by several, and at 20–40
bps to the sigma that is a source of alerts for moves that did not happen. Those fifteen
stay on a consolidated feed.

`source` in `config/basket.yaml` names the store — `asset_id` and the file on disk are
built from it, so it never changes when the fetch moves. `provider` is who is asked, and
changes freely.

---

## The modules

**Data in**
`bars` (Parquet store, sharded by year) · `backfill` (fetch and merge, session-aware
skipping, the morning dividend check) · `sessions` (NYSE calendar and the FX reference
week) · `corporate_actions` (ex-dates and splits) · `cboe` + `fred` + `vix` (the daily VIX
series) · `quality` (bar quality gate) · `audit` (coverage table) · `atomic` (write through a
temp file, so a killed run cannot truncate a table in place)

**Per instrument**
`returns` (returns, the gap, winsorization) · `ewma` (the long-run sigma) · `zscore` (the
short-memory scale) · `pipeline` (assembles the above, extending stored metrics rather
than rebuilding them, and re-scoring the last two days in case a bar has been completed or
corrected since)

**The jump detector**
`jumps` (stage 0: the half-year bipower score and the words)

**The running detector**
`cross_section` (the leave-one-out block factor and the block move) · `basket` + `blocks`
(configuration, block membership, block-level events) · `residuals` (the block
regression, the hour and gap-kind scales, BMP) · `gaps` (the overnight gap's own path) ·
`severity` (the ladder tables, the record lookup) · `saed` (events, the once-a-day rule,
the floor, warm runs) · `persistence` (did the move hold) · `routing` (channel and digest
slot)

**Bookkeeping**
`versioning` (config and run hashes; the code, parsed, not the bytes) · `windows` (every
window and constant of the running detector, in one file) · `saed_score` (after-the-fact
scoring, run by hand) · `feedback` (recorded verdicts)

**Delivery** lives in `price_monitor/`: `tremor_delivery` (renders and sends; decides
nothing, routing is already stamped), `follow_up` (the check-ins that edit a push already
sent), `weekly_digest` (the economic-calendar forecast), `health`, `notifier`, and the
source clients (`tiingo`, `yahoo`, `coinbase`, `twelvedata`, `dukascopy`, `hfdata`) that
`backfill` fetches through.

Product pushes go to `TELEGRAM_CHAT_ID`. Health and named provider failures go to
`TELEGRAM_HEALTH_CHAT_ID`, falling back to the product chat until that secret exists.

---

## Where the data lives

```
data/tremor/bars/                  hourly bars, one Parquet per instrument per year  TRACKED
data/tremor/vix/                   daily VIX close                                   TRACKED
data/tremor/corporate_actions.csv  ex-dates and splits                               TRACKED
data/tremor/dividend_checks.csv    how far each fund's dividends are confirmed       TRACKED
data/tremor/sessions/              the NYSE schedule                                 TRACKED
data/state.json                    what has been sent, and the open note             TRACKED
data/tremor/metrics/, residuals/   per-instrument metrics and ladders         gitignored
data/tremor/saed_events.parquet    routed events — what delivery reads        gitignored
data/tremor/record_book.parquet    the "biggest since" lookup, for warm runs  gitignored
data/tremor/jumps.parquet          stage 0's flagged hours                    gitignored
config/basket.yaml                 the instruments, blocks, floors and the detector settings
config/config.yaml                 the mute and the health thresholds
```

Only the bars are committed. Everything computed from them is gitignored; the hourly job
keeps the metrics and the record book in the Actions cache and extends them, and rebuilds
from the bars only when that cache is missing or `config_version` has moved — about a
minute and a half. The bar archive is the only thing here that cannot be rebuilt. It
reaches 2003 for FX, assembled from Dukascopy because no free plan serves that history.
