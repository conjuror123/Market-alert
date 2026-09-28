# How Tremor works

> **Holds** how a bar becomes a message: the mechanism, the modules, the data layout.
> **Does not hold** why a choice was made (`decisions.md`), how to run it
> (`operations.md`), or the command order (`CLAUDE.md`, which is canonical).
> **Describe what runs.** If a paragraph argues for something, it belongs in
> `decisions.md`.

61 instruments — 60 in the basket plus `DBC` tracked outside it — one hourly pass, and a
message only when one of them moves unusually **for itself**.

**This branch is the jump detector, as it will run live.** It is built in stages on
`claude/youthful-pascal-u0rx7u`; production keeps running the previous detector from its own
branch until the switch, which comes once stages 3 and 4 exist. The previous detector's code
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
date for currency pairs and coins (`sessions.day_tz`).

**Stage 2, channels — built.** `high`, `major` and `extreme` push: a message of their own,
at once. `noticeable` goes into the weekly note, with a small ping that points at it
(`routing.PUSH_TIERS`, `jumps.for_delivery`). There is **one note a week**, opened Saturday
00:05 UTC and edited in place until the next Saturday. The coming week's economic calendar
goes out as its own message in the same run, just before the note opens. A jump message
says only what the detector measured: the colour of the square is the word, and the size is
`|move| / σ` over the last half-year — the hour's σ, or the night's or the weekend's for a
gap — at the end of the first line:

```
🟥 LTC-USD · Litecoin +5.76% · 11.0×σ
🕐 24.09.2026 02:00 UTC
```

and a note row's ping:

```
⬜ LTC-USD · Litecoin +2.89% · 5.3×σ
Added to digest👆🏻👆🏻
```

No "biggest since" date, no check-in lines and no block/own split: those are stages 3, 4 and
8, and come back with them.

The output is `data/tremor/jumps.parquet`, every instrument's events — hour, night or
weekend — with `escalation` marking the ones that raised their day, and the columns the
delivery layer reads (`event_id`, `tier`, `channel`, `digest_slot`, `sigma_lt`). It is
rescored from the whole history every run, in about two seconds, so nothing of it is cached.
`tools/stage_report.py` prints what the detector flags: per week, per instrument and block,
the gaps by kind, how the biggest hours of each record were worded, and the events by
channel.

**Stages to come**, each only after the previous one has been reviewed:

| | stage | source |
|---|---|---|
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

## Delivery

**A push** goes out the hour its event is found, as its own message: the first line and the
hour, and beneath them the scheduled releases in the hours around the move (`Nearby
economic events`) when there are any. It is re-rendered every run and edited in place if
its text changed — the hour is scored a few minutes in and the bar heals on the next fetch
(`follow_up`). Nothing older than 48 hours is sent.

**A note row** goes into the weekly note — one a week, opened Saturday at 00:05 UTC just
after the economic calendar's own message, and edited in place. The note's header carries
the VIX line (the fear gauge, `tremor.vix`). Since Telegram does not notify on an edit, each
row gets a small ping pointing at the note; a ping exists only while the note beneath it
shows its row, and all are deleted as the next note opens.

**The sweep** deletes two kinds of message and no others: pings, and a day's lower
messages once the day has grown and its rarer message is on the channel (each event of such
a day carries `superseded_by`, the day's rarest). Everything else that changes is corrected
in place by an edit (`follow_up`): a push whose bar healed and whose numbers moved, and a
push whose word fell to `noticeable`, which stays
the one message for its move. The bot is an administrator of a public channel; a delete it
is refused anyway is struck through by an edit.

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
series and the fear-gauge line) · `quality` (bar quality gate) · `audit` (coverage table) ·
`atomic` (write through a temp file, so a killed run cannot truncate a table in place)

**Per instrument**
`returns` (the move and the gap) · `pipeline` (assembles them, extending stored metrics
rather than rebuilding them, and re-scoring the last two days in case a bar has been
completed or corrected since)

**The jump detector**
`jumps` (the half-year bipower score, the words, the gaps by kind, one event a day, the
delivery columns) · `routing` (which words push, and the weekly note's slots) · `basket`
(the instruments and the `detector:` settings)

**Bookkeeping**
`versioning` (config and run hashes; the code, parsed, not the bytes) · `windows` (the
pipeline's warm lead and the VIX line's settings) · `ewma` + `zscore` (the VIX line's
long-run sigma and short-memory state)

**Delivery** lives in `price_monitor/`: `tremor_delivery` (renders and sends; decides
nothing, routing is already stamped), `follow_up` (corrects a push already sent, and
deletes a day's lower push), `weekly_digest` (the economic calendar, sent just
before the weekly note opens), `health`, `notifier`, and the source clients (`tiingo`,
`yahoo`, `coinbase`, `twelvedata`, `dukascopy`, `hfdata`) that `backfill` fetches through.

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
data/tremor/metrics/               per-instrument metrics: the move and the gap gitignored
data/tremor/jumps.parquet          routed events — what delivery reads          gitignored
config/basket.yaml                 the instruments, blocks and the detector settings
config/config.yaml                 the mute and the health thresholds
```

Only the bars are committed. Everything computed from them is gitignored; the hourly job
keeps the metrics in the Actions cache and extends them, and rebuilds from the bars only
when that cache is missing or `config_version` has moved — about ten seconds. The events
are rescored whole every run. The bar archive is the only thing here that cannot be rebuilt. It
reaches 2003 for FX, assembled from Dukascopy because no free plan serves that history.
