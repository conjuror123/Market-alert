# How Tremor works

Sixty instruments in the basket, plus `DBC` tracked outside it (61 names), one hourly
pass, and a message only when one of them moves unusually **for itself**. This is the
map: what runs, in what order, and why each piece exists. For *why* particular choices
were made, see `decisions.md`. For running it, see `operations.md`.

---

## The idea in one paragraph

A 1.5% hour is nothing in Solana and a once-a-year event in short Treasuries, so a
single percentage threshold across a basket says almost nothing. Tremor measures every
instrument against **its own history** and reports a **return period** — "the biggest
move in about three years" — which means the same thing everywhere. Before asking how
unusual a move is, it subtracts what the whole market did, so that "gold moved" and
"everything moved, gold included" are different messages.

---

## The hourly pass

Six commands, in this order, and the order is load-bearing.

```
price_monitor.floor          apply any /floor command before anything is scored
tremor.backfill              fetch new bars from the sources into data/tremor/bars/
tremor.pipeline              per-instrument metrics: returns, volatility, quality gate
tremor.saed                  residuals, the ladder, events, routing   <- the product
price_monitor.floor --reply  answer /floor once this run has scored it
price_monitor                deliver whatever is due to Telegram
```

`saed` reads what `pipeline` wrote and builds its cross-section in memory — that is why
`cross_section` is a module and not a step. `floor` runs twice for one reason: the yaml
edit has to land before `saed` so the same hour uses the new number, and the reply has to
wait until after it, because the events table is gitignored and a fresh runner has
nothing to count until this run writes it. `price_monitor` runs last because delivery
reads `saed_events.parquet` off disk and must read the file this run just wrote.

Everything between the bars and the events is derived and gitignored. It rebuilds from
the bars in about two minutes, which the hourly job does anyway.

---

## From a bar to a message

**1. The return.** `r = ln(close / open)` — inside the hour. The overnight gap is a
separate channel (`r_gap`) and is deliberately excluded: a gap is not something the
detector claims to see, and an ex-dividend drop lands there rather than in `r`.

**2. Remove the market.** `r = alpha + beta·F + e`, a rolling regression on the basket
factor and the instrument's own block factor, fitted on the previous 500 bars ending
three bars before the one being judged. The residual `e` is what the rest of the market
did not explain. This is the **market model** of the event-study literature; the
estimation gap keeps a move from leaking into the estimate of normal.

**3. Standardise across the hour.** The residual is divided by the spread of *the other
instruments'* standardised residuals that same hour — the BMP statistic, leave-one-out
so a genuine single-asset move cannot inflate the bar it is measured against. With few
peers this is a t rather than a z, so it goes through a normalising transform before
anything reads it as a z-score.

**4. Rank it against its own history.** Two ladders run in parallel, both fitted per
instrument on an expanding window and applied only forward:

| | |
|---|---|
| **abnormal** | how rare is this *residual*, for this instrument |
| **absolute** | how rare is this *raw move*, for this instrument |

Each yields a tier by return period: `routine` (a fortnight), `notable` (two months),
`major` (a year), `extreme` (three years). An hour clearing both is reported at its
rarest tier and marked `both`. A level is simply the biggest move in that rung's own
lookback — nothing fitted, nothing extrapolated — so clearing it means exactly what the
message says: *the biggest move since 3 March 2020*.

**5. Sanity gates.** A move smaller than two ticks is not a small event but an
unobserved one, and is dropped. An hour claimed by the abnormal channel *alone* that
Corrado's rank test contradicts has that claim withdrawn — the rank test estimates no
variance, which is the quantity thin trading corrupts.

**6. One event, not many.** A cooldown of twelve bars folds repeats into one event,
which keeps the highest tier and the biggest bar it saw.

**7. Route it.** `major` and `extreme` interrupt at once; everything else goes into the
running digest note. Nothing waits and nothing is dropped — retention no longer decides
whether an event is sent, only what the sent message says about it. Pushes belonging to
one episode collapse into the first of them, unless a later one is rarer.

Every alert then splits the move into the three things it can be, adding back to the move
exactly: the whole basket drifting, the instrument's own block, and the instrument itself.
The word *market* is deliberately absent — for the S&P 500 the market *is* the S&P 500, and
there is no index being followed. The block is split out because two parts was sometimes
wrong: on 2008-11-20 the financial sector's basket beta was negative and its +10.50% came
almost entirely from the equity block. Each line names the instruments it is talking about:
the block line lists the peers the factor is a leave-one-out median of, and a footer lists
all sixty-one tracked instruments by block. Measured across the record the basket carries
a median **13%** of the three-way spread against the block's **37%** — smallest of the
three, but not nothing, and it ranges from 10% on the S&P 500 to 32% on high-yield credit,
whose own block explains almost nothing about it. Beside it the alert gives the size against
the instrument's usual hour and **the date it was last this rare**. The frequency wording
("about once every three years") was removed: a lookback ladder can support a record
claim and cannot support a rate, and `saed_score` no longer scores that rate. The
0.45× / 1.75× table in `data/tremor/evaluation.md` is a frozen artifact of the old
wording; see `docs/decisions.md`.

**8. Deliver.** A push the hour it is found, speaking for its whole episode: a move folded
into it does not buzz again and takes no row of its own anywhere — the push lists it with
its size, which matters because a folded companion moved *more* than the push that spoke
for it 49% of the time. A digest row into the note for its period,
also the hour it is found — the note is *opened* Monday and Saturday at 00:05 UTC and
edited in place for the rest of the period. The edit is silent, so each row also sends a
throwaway ping that is deleted when the next note opens. A note may only be opened in its
own hour or the three after it; a period that misses that window is picked up by the next
note, so the boundaries hold and no move is dropped. Both kinds of message are then corrected
as the market answers: a push at this day's close and the next day's close, a digest row at
the next day's close. A push older than 48 hours is never sent.

---

## One detector, and the two that were removed

`saed.py` is the product and the only detector that runs. Two others were built,
measured and deleted rather than left switched off:

- **SI-Index** asked whether the whole basket was moving together. It ran hourly for
  months and no module ever read its output — not delivery, not the digest. Deleted.
- **A volatility-regime forecast** asked whether tomorrow would be turbulent. The
  original specification wanted "something extraordinary is about to happen to one of
  these blocks"; measured walk-forward that question scores an R² of **0.02** — very
  nearly no forecastable structure at this horizon. The same machinery predicting
  *basket* volatility scores 0.20 against a naive 0.10, but nothing consumed it either.
  Deleted.

Both findings are kept in `decisions.md`; the code is in git history. What survives of
the block-level idea is block events in `blocks.py`, which are delivered.

---

## The modules

**Data in**
`bars` (Parquet store, sharded by year) · `backfill` (fetch and merge, session-aware
skipping) · `sessions` (NYSE calendar and the FX reference week) · `corporate_actions`
(declared ex-dates and splits) · `cboe` + `fred` + `vix` (the VIX series) · `quality`
(data gate, tick resolvability) · `audit` (coverage table) · `atomic` (write through a
temp file, so a killed run cannot truncate a table in place)

**Per instrument**
`returns` (returns, gap channel, winsorization) · `zscore` (adaptive EWMA) ·
`pipeline` (assembles the above, and extends stored metrics rather than rebuilding)

**The detector**
`cross_section` (quorum, the leave-one-out block factor, dispersion) · `basket` +
`blocks` (configuration, block membership, block-level events) · `residuals` (market
model, BMP, rank test) · `severity` (the ladder) · `saed` (events, the once-a-day rule,
the gates) · `persistence` (did it hold) · `routing` (channel and digest slot)

**Bookkeeping**
`versioning` (config and run hashes over content) · `windows` (every window and
constant, in one file) · `evaluate` + `saed_score` (after-the-fact scoring) ·
`feedback` (recorded verdicts)

**Delivery** lives in `price_monitor/`: `tremor_delivery` (messages), `weekly_digest`
(the economic-calendar forecast, and the daily top-up of the archive the pushes read),
`floor` (the /floor command), `follow_up` (the check-ins that edit a push already
sent), `health`, `notifier`, and the source clients
(`tiingo`, `yahoo`, `coinbase`, `twelvedata`, `dukascopy`, `hfdata`) that `tremor.backfill`
fetches through. Product pushes go to `TELEGRAM_CHAT_ID`. Health and named provider
failures go to `TELEGRAM_HEALTH_CHAT_ID`, falling back to the product chat until that
secret exists. Hourly bars are Tiingo / Yahoo / Coinbase; Twelve Data is archive and
gaps; Dukascopy and HF Data deepen history. Corporate actions are declared
`divCash` / `splitFactor` from Tiingo, not inferred from an adjusted/raw ratio.

---

## Where the data lives

```
data/tremor/bars/          hourly bars, one Parquet per instrument per year   TRACKED
data/tremor/vix/           daily VIX close                                    TRACKED
data/tremor/corporate_actions.csv  declared ex-dates and splits               TRACKED
data/tremor/metrics/       per-instrument metrics                    gitignored
data/tremor/residuals/     residuals, ladders, tiers                 gitignored
data/tremor/saed_events.parquet    routed events — what delivery reads   gitignored
data/tremor/saed_block_alerts.parquet  block-level events              gitignored
config/basket.yaml         the instruments
config/config.yaml         the mute and the health thresholds
```

The bar archive is the only thing here that cannot be rebuilt: it reaches 2003 for FX,
assembled from Dukascopy because no free plan serves that history. Everything else is a
function of it.
