# Market Alert

Once an hour the bot looks at a basket of sixty instruments — equities, credit,
rates, precious and industrial metals, energy, agriculture, FX and crypto — and
writes to Telegram when one of them moves in a way that is unusual **for that
instrument**.

That last part is the whole design. A 1.5% hour is nothing in SOL and a
once-a-year event in short Treasuries, so a single percentage threshold shared
across a basket says almost nothing. Each instrument is measured against its own
history, and what comes out is a return period — "the biggest move in about six
years" — which means the same thing everywhere and needs no calibration intuition
to read.

Measured over 23 years of hourly history (2.9M bars, cold pass on the current code):

| | |
|---|---|
| pushes | 1,297 — about **56 a year**, on 35 interrupted days |
| reached you | **97.2%** of the 216 hours that were unmistakably large for their own instrument; 6 silent |
| false alarms | **0.008%** — 93 of 1.17M hours below their instrument's median move |
| held up | **72%** of pushes still standing at the next close |

That rate is an OUTPUT, watched rather than aimed at. Nothing caps it, and the
rungs are not tuned against it: they are tuned against what each word should mean
for a single instrument, so the yearly total is whatever sixty instruments of that
sensitivity happen to produce. Widening the basket raises it, and that is not a fault.

It runs entirely on GitHub Actions. Nothing extra needs hosting.

## Quick start

1. Create a Telegram bot through [@BotFather](https://t.me/BotFather) and get a token.
2. Decide where it should write:
   - **To yourself** — send the bot `/start`, then read your `chat_id` from
     `https://api.telegram.org/bot<TOKEN>/getUpdates`.
   - **To a channel**, if others should be able to subscribe. Create it, add the bot
     as an administrator with the right to post; `chat_id` is then the `@username`.
3. **Settings → Secrets and variables → Actions → Secrets**:

   | secret | needed for |
   |---|---|
   | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | required — where pushes go |
   | `TELEGRAM_HEALTH_CHAT_ID` | optional — health and provider failures; falls back to the product chat. `/floor` is read here |
   | `TIINGO_API_KEY` | 29 US-session funds + 8 FX pairs (free: 50/hour, 1000/day) |
   | `TWELVEDATA_API_KEY` | archive, gap-fill and deepening — **not** the hourly path |
   | `FRED_API_KEY` | the VIX series only |
   | `HFDATA_API_KEY` | optional — deepening US-equity history before 2020 |

   Yahoo (15 thin ETFs) and Coinbase (9 crypto) need no key. Without a Tiingo key the
   hourly run still prices the Yahoo and Coinbase names and stays silent on the rest.
4. **Set up the hourly trigger — mandatory.** GitHub's own `schedule:` was measured
   firing about once every ten hours on this repository, so an external free cron
   service calls `workflow_dispatch` through the API instead. See `docs/operations.md`.

To check the setup without waiting for a signal: **Actions → Test Telegram
Notification → Run workflow**.

## Which instruments are tracked

Sixty in the basket plus `DBC` (broad commodities) tracked outside it — 61 names in
nine blocks. A block is the group an instrument is compared against: the detector
removes what the block did before asking whether the instrument moved on its own.

| Block | Instruments |
|---|---|
| FX | Euro / dollar, Dollar / yen, Pound / dollar, Australian dollar / dollar, New Zealand dollar / dollar, Dollar / franc, Dollar / Canadian dollar, Dollar / offshore yuan |
| equity | The eleven GICS sector funds; S&P 500, Nasdaq 100, Russell 2000, developed markets ex-US, emerging markets |
| rates | Treasuries 1-3y, 3-7y, 7-10y, 10-20y, 20+y, inflation-protected Treasuries, mortgage-backed securities |
| credit | Investment-grade and high-yield corporates (two indices), EM dollar sovereigns, senior bank loans, preferred shares |
| crypto | Bitcoin, Ethereum, Solana, Litecoin, Bitcoin Cash, Chainlink, Cardano, Dogecoin, Avalanche |
| energy | WTI crude, Brent crude, gasoline, natural gas |
| precious_metals | Gold, Silver, Platinum, Palladium |
| industrial_metals | Base metals, Copper |
| agriculture | Broad agriculture, Corn, Wheat, Soybeans |

The list is `config/basket.yaml`, one entry per instrument with its source, provider,
tick size and trading calendar. There are no per-instrument thresholds to set — the
ladder is fitted from each instrument's own history, which is the point.

**Adding one** means adding the entry and running `python -m tremor.backfill`. Three
things to know first. A rung is the biggest move in its own lookback, so a new
instrument can only claim one it has lived — silent at `extreme` until it is six years
old, growing a rung at a time. The hourly providers are Tiingo, Yahoo and Coinbase;
Twelve Data's 800/day is archive work. And for a US-listed fund that pays dividends,
`python -m tremor.corporate_actions` needs rerunning: pre-2020 deepening unadjusts HF
Data's consolidated tape against those ex-dates, and without them the check fails and
the deepening is skipped rather than wrong.

## How it decides

**1. The return.** `r = ln(close / open)` — inside the hour. The overnight gap is a
separate channel and deliberately excluded: a gap is not something the detector claims
to see, and an ex-dividend drop lands there rather than in `r`.

**2. Remove the market.** `r = alpha + beta·F + e`, a rolling regression on the
instrument's own block factor, fitted on the previous 500 bars ending three bars before
the one being judged. The residual `e` is what the block did not explain. The estimation
gap keeps the move itself from leaking into the estimate of normal.

**3. Standardise across the hour.** The residual is divided by the spread of *the other
instruments'* standardised residuals that same hour — the BMP statistic, leave-one-out,
so a genuine single-asset move cannot inflate the bar it is measured against.

**4. Place it on the ladder.** A rung is literally the biggest move in its own lookback
— nothing fitted, nothing extrapolated — so the claim "biggest in about three years" is
exactly true of the archive rather than an estimate from a tail model.

**5. Route it.** Four rungs — noticeable, high, major, extreme, at a month, a quarter,
three years, six years. `major` and `extreme` interrupt; the other two collect in a
digest. One event per instrument per trading day, and no more.

Two channels ask different questions, on the same ladder:

| channel | asks |
|---|---|
| **absolute** | was this simply a big move for this instrument |
| **abnormal** | was it big *after* subtracting what its block did |

Both run at block level too: did a whole sector move together, cleaned of the rest of
the market. That is why "gold moved" and "everything moved, gold included" are
different messages.

## The hourly pass

Six commands, in this order, and the order is load-bearing.

```
price_monitor.floor        apply any /floor command before anything is scored
tremor.backfill            fetch new bars into data/tremor/bars/
tremor.pipeline            per-instrument metrics: returns, volatility, quality gate
tremor.saed                residuals, the ladder, events, routing   <- the product
price_monitor.floor --reply  answer /floor once this run has scored it
price_monitor              deliver whatever is due to Telegram
```

`saed` reads what `pipeline` wrote and builds its cross-section in memory.
`price_monitor` runs last because delivery reads `saed_events.parquet` off disk and must
read the file this run just wrote. Everything between the bars and the events is derived
and gitignored; it rebuilds from the bars in about two minutes, which the job does anyway.

## Where the bars come from

Chosen per instrument by measurement, not by preference — each candidate was compared
against the stored bars hour by hour, in basis points. `docs/tiingo-findings.md` has the
table.

| provider | names | why |
|---|---|---|
| Tiingo | 37 | 8 FX pairs + the funds whose single-exchange price matches the tape |
| Yahoo | 15 | the thin funds where one exchange is *not* the same price |
| Coinbase | 9 | crypto |
| Twelve Data | — | archive, gap-fill, deepening. Not on the hourly path |

Twelve Data's free tier forces an eight-second pause between symbols, and for 52 symbols
that pause *was* the run. Moving off it took a hourly job from ~350s to ~125s.

`source` in the basket names the store — the asset id and the file on disk are built
from it, so it never changes when the fetch moves. `provider` is who is asked, and that
can change freely.

## The repository

```
config/basket.yaml         basket composition: 61 names, 9 blocks, tiers, price steps,
                           per-instrument and per-block size floors

Data in
tremor/bars.py             the hourly bar store, Parquet sharded by year
tremor/backfill.py         fetch and merge from every source, session-aware skipping
tremor/sessions.py         the NYSE calendar and the basket's reference week
tremor/corporate_actions.py  ex-dates and splits, from Tiingo's declared amounts
tremor/cboe.py, fred.py, vix.py   the daily VIX series
tremor/quality.py          the bar quality gate, tick resolvability
tremor/audit.py            the data coverage table

Per instrument
tremor/returns.py          returns, the gap channel, winsorization
tremor/zscore.py           the out-of-sample adaptive EWMA Z-score
tremor/pipeline.py         assembles the above into per-instrument metrics

The detector
tremor/cross_section.py    quorum, the leave-one-out block factor, dispersion
tremor/residuals.py        the market model on the block factor
tremor/severity.py         the rarity ladder: a rung is the biggest move in its lookback
tremor/saed.py             events, the once-a-day rule, the gates
tremor/persistence.py      did the move hold at the next close
tremor/routing.py          channel and digest slot
tremor/blocks.py           block-level events

Bookkeeping
tremor/versioning.py       config_version and run_version, hashes over content
tremor/windows.py          every window and constant, in one file
tremor/atomic.py           write-through-temp-file, so a killed run cannot truncate
tremor/evaluate.py, saed_score.py   after-the-fact scoring
tremor/feedback.py         recorded verdicts

Delivery — price_monitor/
tremor_delivery.py         renders and sends; decides nothing, routing is already stamped
floor.py                   the /floor command: reads Telegram, edits basket.yaml
follow_up.py               the check-ins that edit a push already sent
weekly_digest.py           the economic-calendar forecast, Saturday
health.py, notifier.py     failure reporting
tiingo.py, yahoo.py, coinbase.py, twelvedata.py, dukascopy.py, hfdata.py
                           the source clients tremor.backfill fetches through

data/tremor/bars/                  hourly bars, one Parquet per instrument per year  TRACKED
data/tremor/corporate_actions.csv  ex-dates and splits                               TRACKED
data/tremor/sessions/              the NYSE schedule                                 TRACKED
data/tremor/vix/                   the daily VIX series                              TRACKED
data/tremor/metrics/, residuals/   gitignored — rebuilt from the bars in ~2 minutes
data/tremor/saed_events.parquet    gitignored — Parquet git cannot delta

docs/architecture.md       how it works, the hourly pass in order
docs/decisions.md          why, with the measurement that settled each choice
docs/operations.md         running it, quotas, what breaks and how you would know
docs/working-agreement.md  the rules an agent changing this repository works under
docs/tiingo-findings.md    the provider comparison, measured
docs/TZ_MEALS_v5.1.txt     the original specification: what every "§4.3" points at
```

**The specification is superseded.** It is kept because the code cites its section
numbers 234 times across 35 files, but where it and the measurements disagree, the
measurements win. It is deliberately **not** in `versioning.CONFIG_INPUTS`: a typo in
its prose must not change `config_version`.

The system was called MEALS until September 2026. The spec keeps the old name in its
filename, because renaming a document somebody else wrote is a different thing from
renaming a program. It was written in Russian and is kept in translation with the
section numbering, formulas and constants untouched; the Russian original is commit
`8b2fb5a` (`git show 8b2fb5a:docs/TZ_MEALS_v5.1.txt`) and stays the authority if a
translated sentence ever reads two ways.

## Running locally

```bash
pip install -r requirements-dev.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
export TIINGO_API_KEY=...      # hourly bars
export TWELVEDATA_API_KEY=...  # archive / gap-fill / deepening
export FRED_API_KEY=...        # the VIX series only

python -m tremor.backfill            # fetch new bars
python -m tremor.pipeline            # per-instrument metrics
python -m tremor.saed                # events
python -m price_monitor              # deliver whatever is due
```

Occasional maintenance, not part of the hourly run:

```bash
python -m tremor.audit               # the coverage table
python -m tremor.sessions            # regenerate the NYSE schedule
python -m tremor.corporate_actions   # rebuild the ex-dividend / split table
python -m tremor.evaluate            # score the detector (refuses to overwrite
                                     # the tracked report without --force)
python -m price_monitor.economic_calendar --rebuild
```

Tests: `pytest -q`.

## Turning it up or down

Two knobs in `config/basket.yaml`, answering different questions:

```yaml
sensitivity: 1.0      # how RARE must a move be before it is worth a line
min_move_sigma: 1.0   # how BIG must it be, in its own terms, whatever the rarity
```

`sensitivity` scales all four rungs together: `2.0` makes every rung twice as rare and
the messages roughly half as many. It does **not** equalise instruments — each is still
judged against its own history, so `SHY` may speak once a year and `SOL` a hundred
times. That spread is the point of a return period, not a fault to normalise away.

`min_move_sigma` is the size floor. The abnormal channel asks whether a move was
*unexplained*, never whether it was *large*, so without a floor an instrument that
ticked +0.03% while its block went the other way could be reported as a once-a-month
event. In units of the instrument's own sigma, so it means the same to `SHY` as to `SOL`.

**Neither number is guessable from the data.** A message that was not worth reading is
turned down from a private chat with the bot, effective on the next hourly run:

```
/floor BKLN 2.5
/floor Base metals 2.2
```

That writes `min_move_sigma` on the instrument, or `block_min_move_sigma` on the block's
own line, as typed — even when the new number is smaller than the floor already there.
A block command does not copy the number onto its members. The bot replies with what it
set and how often a line at that size has opened on the stored events (unique trading
days, not raw hours).

`python -m tremor.feedback --missed "GLD 2026-09-11 14:00"` records a move that should
have arrived and did not. Verdicts live in `data/tremor/feedback.csv`.

The asymmetry is deliberate: you can point at a message that arrived and should not
have, and cannot point at one that never came. So the system errs loud and is turned
down from recorded judgements, rather than erring quiet and never learning what it
swallowed.

## Limitations

- **Nothing notices silence.** The health check counts consecutive *failures*, and only
  a run that executes can increment one. `price-monitor.yml` has no schedule of its own,
  so if the external trigger stops, nothing says so — it stopped on 2026-09-01 and took
  six days to notice. A watchdog is designed and not yet built.
- **The ladder cannot claim a return period longer than its history.** A newly added
  instrument says "biggest in a quarter" for years before it can say "biggest in six",
  and says nothing at all for the first two. Five instruments cannot reach the top rung
  today for that reason; thirteen stop at 2020 on a provider plan limit.
- **Tiingo's hourly bucket is the binding live limit** — 50 requests an hour against 37
  instruments. US-session names skip when the NYSE calendar says no bar can have
  appeared; FX skips when the Sun 17:00 → Fri 17:00 New York week is shut.
- **Yahoo is an undocumented endpoint.** No SLA; it can change shape without notice.
  That is why the funds are split across two providers rather than sent to one.
- **It does not predict, and does not claim to.** Every number is about a move that
  already happened; the tier says how unusual it was, not what comes next.
