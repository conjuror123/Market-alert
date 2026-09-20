# How Tremor works

> **Holds** how a bar becomes a message: the mechanism, the modules, the data layout.
> **Does not hold** why a choice was made (`decisions.md`), how to run it
> (`operations.md`), or the command order (`CLAUDE.md`, which is canonical).
> **Describe what runs.** If a paragraph argues for something, it belongs in
> `decisions.md`.

61 instruments — 60 in the basket plus `DBC` tracked outside it — one hourly pass, and a
message only when one of them moves unusually **for itself**. This is what runs and what
each piece does.

---

## The idea

A 1.5% hour is nothing in Solana and enormous in short Treasuries, so a single
percentage threshold across a basket says almost nothing. Every instrument is measured
against **its own history**, and the message carries a date — *the biggest move since 3
March 2020* — which means the same thing everywhere. Before asking how unusual a move is,
the system subtracts what the instrument's block did, so that "gold moved" and "everything
moved, gold included" are different messages.

---

## The hourly pass

**Six commands, and the order is load-bearing — `CLAUDE.md` lists them and is the only
copy.** It is there rather than here because an agent is handed that file automatically,
and a second copy of a load-bearing order is a second copy that can drift.

What matters for reading the rest of this document: `saed` reads what `pipeline` wrote and
builds its cross-section in memory, which is why `cross_section` is a module and not a
step. Everything between the bars and the events is derived and gitignored; it rebuilds
from the bars in about two minutes.

---

## From a bar to a message

**1. The return.** Inside the hour. The first bar of a session is measured from its own
open, `r = ln(close / open)`; every other bar is close to close. So the overnight jump is
not a return at all — a gap is not something the detector claims to see, and an
ex-dividend drop falls in the jump rather than in `r`.

**2. Remove the block.** `r = alpha + beta·F + e`, a rolling 500-bar regression on the
instrument's own block factor, ending three bars before the bar being judged. The residual
`e` is what the block did not explain; the estimation gap keeps the move itself out of the
estimate of normal.

**3. Standardise across the hour.** The residual is divided by the spread of *the other
instruments'* standardised residuals that same hour — the BMP statistic, leave-one-out, so
a genuine single-asset move cannot inflate the bar it is measured against. With few peers
this is a t rather than a z, and a normalising transform maps it to z for the degrees of
freedom actually present.

**4. Rank it against its own history.** Two ladders run in parallel, both per instrument
on an expanding window applied only forward:

| | |
|---|---|
| **absolute** | how rare is this *raw move*, for this instrument |
| **abnormal** | how rare is this *residual*, for this instrument |

Four rungs — `noticeable`, `high`, `major`, `extreme` — each a multiple of the
instrument's own long-run sigma, set per block, and spaced about 1.5x apart. The rung is
a size; the DATE in the message is read off the record separately, which is why clearing
a rung means exactly what the message says: *the biggest move since 3 March 2020*. An
hour clearing both ladders is reported at its rarer tier and marked `both`.

The rungs are a preference rather than a frequency target — see `docs/decisions.md` for
what that costs. Measured over the archive, one instrument reaches them about every 2
months, 6 months, 17 months and 2.9 years.

Blocks are ranked the same way on their own median series, so a whole sector moving
together is its own event with its own ladder.

**5. Gates.** A move smaller than two ticks is unobserved rather than small, and is
dropped. A per-instrument or per-block size floor (`min_move_sigma`) drops moves that are
unexplained but trivial. An hour claimed by the abnormal channel *alone* that Corrado's
rank test contradicts has that claim withdrawn.

**6. One event per instrument per trading day.** End of session for the funds, end of the
UTC day for crypto. An instrument that moves again the same day updates the event it
already has — keeping the higher tier and that bar's numbers — rather than opening a
second.

**7. Route it.** `major` and `extreme` interrupt at once. `noticeable` and `high` go into
the running digest note. Nothing waits and nothing is dropped: retention decides what the
sent message says, not whether it is sent.

**8. Deliver.** A push goes out the hour it is found and is final when it arrives. A
digest row goes into the note for its period, also the hour it is found — the note is
*opened* Monday and Saturday at 00:05 UTC and edited in place for the rest of the period.
Telegram does not notify on an edit, so each row also sends a throwaway ping pointing at
the note; a ping exists only while the note beneath it shows its row, and is deleted
otherwise. A note may only be opened in its own hour or the three after; a period that
misses that window is carried into the next note, so no move is dropped. Messages are then
corrected as the market answers: a push at this day's close and the next day's close, a
digest row at the next day's close. Nothing older than 48 hours is sent.

Each alert splits the move into the two things it can be, adding back to it exactly: the
instrument's own block, and the instrument itself. Each line names what it is talking
about — the block line lists the peers its factor is a leave-one-out median of. Beside it
the alert gives the size against the instrument's usual hour and the date it was last
this rare.

---

## Where the bars come from

Chosen per instrument by measurement: each candidate was compared against the stored bars
hour by hour in basis points, against a yardstick of 20–40 bps for one sigma of an hourly
move.

| provider | names | role |
|---|---|---|
| Tiingo | 37 | 8 FX pairs + the funds whose single-exchange price matches the consolidated tape |
| Yahoo | 15 | the thin funds where one exchange is *not* the same price |
| Coinbase | 9 | crypto |
| Twelve Data | — | archive, gap-fill and deepening; not on the hourly path |
| Dukascopy, HF Data | — | history below what the live providers reach |

The liquid funds agree with the stored bars to under a basis point. The thin
single-commodity funds do not — on one exchange's prints they drift by several, and at 20–40
bps to the sigma that is a source of alerts for moves that did not happen. Those fifteen
stay on a consolidated feed.

Twelve Data's free tier forces an eight-second pause between symbols; for 52 symbols that
pause was the run, which is why nothing live sits on it any more.

`source` in `config/basket.yaml` names the store — `asset_id` and the file on disk are
built from it, so it never changes when the fetch moves. `provider` is who is asked, and
changes freely.

---

## The modules

**Data in**
`bars` (Parquet store, sharded by year) · `backfill` (fetch and merge, session-aware
skipping) · `sessions` (NYSE calendar and the FX reference week) · `corporate_actions`
(declared ex-dates and splits) · `cboe` + `fred` + `vix` (the daily VIX series) ·
`quality` (bar quality gate, tick resolvability) · `audit` (coverage table) · `atomic`
(write through a temp file, so a killed run cannot truncate a table in place)

**Per instrument**
`returns` (returns, winsorization) · `ewma` (the long-run sigma, exponentially weighted
over a bounded window) · `zscore` (adaptive EWMA) · `pipeline`
(assembles the above, extending stored metrics rather than rebuilding them, and
re-scoring the last two days in case a bar has been completed or corrected since)

**The detector**
`cross_section` (quorum, the leave-one-out block factor, dispersion) · `basket` + `blocks`
(configuration, block membership, block-level events) · `residuals` (market model, BMP,
rank test) · `severity` (the ladder) · `saed` (events, the once-a-day rule, the gates) ·
`persistence` (did the move hold) · `routing` (channel and digest slot)

**Bookkeeping**
`versioning` (config and run hashes; the code, parsed, not the bytes) · `windows` (every window and constant,
in one file) · `evaluate` + `saed_score` (after-the-fact scoring) · `feedback` (recorded
verdicts)

**Delivery** lives in `price_monitor/`: `tremor_delivery` (renders and sends; decides
nothing, routing is already stamped), `floor` (the `/floor` command), `follow_up` (the
check-ins that edit a push already sent), `weekly_digest` (the economic-calendar forecast),
`health`, `notifier`, and the source clients (`tiingo`, `yahoo`, `coinbase`, `twelvedata`,
`dukascopy`, `hfdata`) that `backfill` fetches through.

Product pushes go to `TELEGRAM_CHAT_ID`. Health and named provider failures go to
`TELEGRAM_HEALTH_CHAT_ID`, falling back to the product chat until that secret exists.

---

## Where the data lives

```
data/tremor/bars/                  hourly bars, one Parquet per instrument per year  TRACKED
data/tremor/vix/                   daily VIX close                                   TRACKED
data/tremor/corporate_actions.csv  declared ex-dates and splits                      TRACKED
data/tremor/sessions/              the NYSE schedule                                 TRACKED
data/tremor/feedback.csv           recorded verdicts                                 TRACKED
data/state.json                    what has been sent, and the open note             TRACKED
data/tremor/metrics/, residuals/   per-instrument metrics and ladders         gitignored
data/tremor/saed_events.parquet    routed events — what delivery reads        gitignored
config/basket.yaml                 the instruments, blocks, tick sizes and floors
config/config.yaml                 the mute and the health thresholds
```

The bar archive is the only thing here that cannot be rebuilt. It reaches 2003 for FX,
assembled from Dukascopy because no free plan serves that history. Everything else is
derived from it.
