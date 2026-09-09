# How Tremor works

Twenty-four instruments, one hourly pass, and a message only when one of them moves
unusually **for itself**. This is the map: what runs, in what order, and why each piece
exists. For *why* particular choices were made, see `decisions.md`. For running it, see
`operations.md`.

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

Five commands, in this order, and the order is load-bearing.

```
tremor.backfill       fetch new bars from the sources into data/tremor/bars/
tremor.pipeline       per-instrument metrics: returns, volatility, quality gate
tremor.cross_section  what the basket did this hour: the factor every residual removes
tremor.saed           residuals, the ladder, events, routing        <- the product
tremor.cluster        SI-Index; also ENRICHES metrics_basket_hour.parquet
price_monitor         deliver whatever is due to Telegram
```

`cross_section` reads what `pipeline` wrote. `saed` reads the basket factor.
`cluster` does not merely follow `saed` — it writes eighteen columns *into*
`metrics_basket_hour.parquet`, so skipping it leaves that file stripped rather than
merely stale. And `price_monitor` runs last because the delivery layer reads
`saed_events.parquet` off disk: it must read the file this run just wrote.

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
rarest tier and marked `both`. Levels come from a Generalised Pareto fit above a high
threshold, because a three-year level cannot be read off five years of empirical
quantiles.

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
all twenty-four tracked instruments by block. Measured across the record the basket carries
a median **13%** of the three-way spread against the block's **37%** — smallest of the
three, but not nothing, and it ranges from 10% on the S&P 500 to 32% on high-yield credit,
whose own block explains almost nothing about it. Beside it the alert gives the size against
the instrument's usual hour and **the date it was last this rare** — the same claim as the
return period, in a form that needs no statistics. The tier is said as a frequency (*a move
this big happens about once every three years*) rather than as a record, because the ladder
claims the former and the two contradict each other in a cluster.

**8. Deliver.** A push the hour it is found, speaking for its whole episode: a move folded
into it does not buzz again and takes no row of its own anywhere — the push lists it with
its size, which matters because a folded companion moved *more* than the push that spoke
for it 49% of the time. A digest row into the note for its period,
also the hour it is found — the note is *opened* Tuesday and Friday at 12:00 Israel time
and edited in place for the rest of the period, so the phone buzzes twice a week and
every row after that arrives silently. A note may only be opened in its own hour or the
three after it; a period that misses that window is picked up by the next note, so the
buzz is always at noon and no move is dropped. Both kinds of message are then corrected
as the market answers: a push at this day's close and the next day's close, a digest row at
the next day's close. A push older than 48 hours is never sent.

---

## The three detectors, and which one is live

| | What it asks | Status |
|---|---|---|
| **SAED** (`saed.py`) | did one instrument move unusually for itself | **live** — the product |
| **SI-Index** (`cluster.py`, `si_index.py`) | is the whole basket moving together | runs hourly; **not delivered** |
| **Volatility regime** (`detector.py`, `forecast.py`) | is tomorrow likely to be turbulent | built and measured; **not wired** |

The third is worth explaining. The original specification asked for "something
extraordinary is about to happen to one of these blocks". Measured walk-forward, that
question has an R² of **0.02** — very nearly no forecastable structure at this horizon.
The same machinery predicting *basket volatility* scores **0.20** against a naive
benchmark's 0.10. So the honest product is the smaller claim, and it exists in
`tremor/features.py`, `forecast.py`, `threshold.py` and `detector.py`.

It is **not run by any workflow**. `market_events.parquet` is read by the delivery layer
and written by nothing, so that channel is frozen at whatever was last committed. That is
a deliberate pause, not a bug — turning it on means a new kind of alert — but it is the
one wire left hanging.

---

## The modules

**Data in**
`bars` (Parquet store) · `backfill` (fetch and merge, session-aware skipping) ·
`sessions` (NYSE calendar) · `corporate_actions` (ex-dates from adjusted/unadjusted
ratios) · `fred` + `vix` (the VIX series) · `quality` (data gate, tick resolvability) ·
`audit` (coverage table)

**Per instrument**
`returns` (returns, gap channel, winsorization) · `zscore` (adaptive EWMA) ·
`volume` (robust profile) · `pipeline` (assembles the above)

**Across the basket**
`cross_section` (quorum, factor, CSV, PCA) · `basket` (configuration and blocks)

**The detector**
`residuals` (market model, BMP, rank test, OU fit) · `severity` (the ladder, GPD fit) ·
`saed` (events, cooldown, gates) · `persistence` (did it hold) · `routing` (channel,
collapse, digest slot)

**The other two**
`si_index` + `cluster` (basket-wide) · `features` + `forecast` + `threshold` +
`detector` + `market` (volatility regime)

**Bookkeeping**
`versioning` (config and run hashes over content) · `journal` (decision log) ·
`export` (JSON under a fixed schema) · `truth` + `evaluate` + `calibrate` (scoring) ·
`windows` (every window and constant, in one file)

**Delivery** lives in `price_monitor/`: `tremor_delivery` (messages), `weekly_digest`
(the economic-calendar forecast, and the daily top-up of the archive the pushes read),
`health`, `notifier`, and the source clients
(`twelvedata`, `coinbase`, `dukascopy`, `fxcm`, `hfdata`) that `tremor.backfill` fetches
through.

---

## Where the data lives

```
data/tremor/bars/          hourly bars, one Parquet per instrument   TRACKED
data/tremor/metrics/       per-instrument metrics                    gitignored
data/tremor/residuals/     residuals, ladders, tiers                 gitignored
data/tremor/events/        JSON export                               gitignored
data/tremor/saed_events.parquet    routed events — what delivery reads
data/tremor/metrics_basket_hour.parquet   basket aggregates + cluster's columns
config/basket.yaml         the instruments
config/config.yaml         the mute and the health thresholds
```

The bar archive is the only thing here that cannot be rebuilt: it reaches 2003 for FX,
assembled from Dukascopy because no free plan serves that history. Everything else is a
function of it.
