# Market Alert

Once an hour the bot looks at a basket of sixty instruments — equities, credit,
rates, precious and industrial metals, energy, agriculture, FX and crypto — and
writes to Telegram when one of them moves in a way that is unusual **for that
instrument**.

That last part is the whole design. A 1.5% hour is nothing in SOL and a
once-a-year event in short Treasuries, so a single percentage threshold shared
across a basket says almost nothing. Instead each instrument is measured against
its own history, and what comes out is a return period — "the biggest move in
about three years" — which means the same thing everywhere and needs no
calibration intuition to read.

Measured over twenty-two years of hourly history: about **22 pushes a year**, a
median of ten days between them, and of the hours that were unmistakably large
for their own instrument (its top 0.01%), **none went unreported**.

It runs entirely on GitHub Actions. Nothing extra needs hosting.

## Quick start

1. Create a Telegram bot through [@BotFather](https://t.me/BotFather) and get a token.
2. Decide where the bot should send its notifications. There are two options:
   - **To yourself.** Send the bot `/start`, then find your `chat_id` — for example
     by opening `https://api.telegram.org/bot<TOKEN>/getUpdates` after you have
     written to it.
   - **To a channel**, if you want other people to be able to subscribe. Create a
     public channel and add the bot as an administrator with the right to post.
     In that case `chat_id` is simply the channel's `@username`, with no numeric ID
     to hunt for.
3. Open the repository settings: **Settings → Secrets and variables → Actions →
   Secrets**. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` there.
4. Everything except crypto goes through a separate provider,
   [Twelve Data](https://twelvedata.com/) — equities, rates, credit, commodities and FX,
   fifty-one of the sixty instruments. Register there (email only, no card) and add the
   key alongside the others under the name `TWELVEDATA_API_KEY`. Without it the hourly
   monitoring keeps working on the nine crypto instruments, which need no key, and stays
   silent on everything else.
5. Set up the hourly trigger — this is mandatory, otherwise the bot will never start
   on its own at all, only manually through Run workflow. GitHub Actions' native
   schedule is unreliable on repositories like this one (it can genuinely fire once
   every 5-10 hours), so instead use an external free alarm clock, see "A reliable
   hourly trigger" below (~10 minutes of setup).

To check the setup without waiting for a real signal: **Actions → Test Telegram
Notification → Run workflow** — it sends a test message and nothing else.

## Which instruments are tracked

Sixty, in nine blocks. The blocks are not decoration: the detector removes what the
whole market did, and then what the instrument's own block did, before asking whether
it moved on its own — a block is the group it is compared against.

| Block | Instruments |
|---|---|
| FX | Euro / dollar, Dollar / yen, Pound / dollar, Australian dollar / dollar, New Zealand dollar / dollar, Dollar / franc, Dollar / Canadian dollar, Dollar / offshore yuan |
| equity | Technology, Financial, Consumer discretionary, Consumer staples, Energy, Health care, Industrial, Materials, Utilities, Real estate and Communication services sectors; S&P 500, Nasdaq 100, Russell 2000 (small caps), developed markets outside the US, emerging markets |
| rates | Treasuries 1-3y, 3-7y, 7-10y, 10-20y and 20+ years, inflation-protected Treasuries, mortgage-backed securities |
| credit | Investment-grade and high-yield corporate bonds (two high-yield indices), emerging-market dollar sovereigns, senior bank loans, preferred shares |
| crypto | Bitcoin, Ethereum, Solana, Litecoin, Bitcoin Cash, Chainlink, Cardano, Dogecoin, Avalanche |
| energy | WTI crude oil, Brent crude oil, gasoline, natural gas |
| precious_metals | Gold, Silver, Platinum, Palladium |
| industrial_metals | Base metals (aluminium, zinc, copper), Copper |
| agriculture | Broad agriculture, Corn, Wheat, Soybeans |

The list lives in `config/basket.yaml`, one entry per instrument, with its source,
tick size and trading calendar. There are no per-instrument thresholds to set — the
ladder is fitted from each instrument's own history, which is the point.

**Adding one** means adding the entry and running `python -m tremor.backfill` to
acquire its history. Three things to know before you do. Its ladder stays empty for
two years of bars and can only claim a return period as long as the history it has,
so a new instrument is quiet at first and then conservative for a while. The
Twelve Data free tier is 8 requests a minute and 800 a day, which the current basket
already uses about 293 of. And for a US-listed equity or ETF that pays dividends,
`python -m tremor.corporate_actions` needs rerunning too — the pre-2020 deepening in
`tremor.backfill` unadjusts HF Data's consolidated tape against these ex-dates before
trusting it, and without them for the new ticker that check fails and the deepening is
silently skipped rather than wrong.

## Tremor — how it decides

Tremor is built to a separate technical specification, version 5.1. It replaced an
earlier detector — sixteen assets, one shared pair of z-score thresholds, no notion
of how rare a move was — rather than refining it. Three things run every hour, and
only one of them is delivered:

| | asks | status |
|---|---|---|
| **SAED** (`saed.py`) | did one instrument move unusually for itself | **live** — the product |
| **SI-Index** (`cluster.py`, `si_index.py`) | is the whole basket moving together | runs hourly, **not delivered** |
| **Volatility regime** (`detector.py`, `forecast.py`) | is tomorrow likely to be turbulent | built and measured, **not wired** |

The third is worth a word. The original specification asked for a forecast of which
block was about to move; measured walk-forward that question has an R² of 0.02 —
close to no forecastable structure at that horizon. The same machinery predicting
next-day *basket volatility* scores 0.20 against a naive benchmark's 0.10, so that is
the smaller, honest claim the code makes instead — and it is a deliberate pause, not a
bug, that nothing yet reads its output.

The system was called MEALS until September 2026. See "Versions and
reproducibility" below for why the original specification keeps that name.

The detector it replaced is gone — deleted, not disabled. What it did badly, and every
decision made since in its place, is on the record in `docs/decisions.md`: one
threshold shared by every instrument, so a 1.5% hour meant the same thing in short
Treasuries as in Solana; nothing to say how rare a move was once it fired; and no way
to tell an instrument moving on its own from the whole market moving together.

The pieces:

```
config/basket.yaml    — basket composition: 60 assets, 9 blocks, tiers, price steps

Data in
tremor/bars.py         — the hourly bar store, Parquet sharded by year, and the hourly grid
tremor/backfill.py     — fetch and merge from every source, session-aware skipping
tremor/sessions.py     — the NYSE calendar and the basket's reference calendar
tremor/corporate_actions.py — ex-dates derived from the adjusted/unadjusted price ratio
tremor/fred.py, tremor/vix.py — the daily VIX series
tremor/quality.py      — the bar quality gate, tick resolvability
tremor/audit.py        — the data coverage table

Per instrument
tremor/returns.py      — returns, the gap channel, winsorization
tremor/zscore.py       — the out-of-sample adaptive EWMA Z-score
tremor/volume.py       — a robust volume profile by local exchange hour
tremor/pipeline.py     — assembles the above into per-asset metrics

Across the basket
tremor/cross_section.py — quorum, the leave-one-out block factor, basket dispersion
tremor/basket.py, tremor/blocks.py — the instrument list and block membership

The detector (SAED — live, the product)
tremor/residuals.py    — the market model, on the block factor alone: a basket-wide
                          factor was tried and dropped, for the reason a block factor
                          works and a basket one did not scale to sixty instruments
tremor/severity.py     — the rarity ladder, Generalised Pareto fit above a high threshold
tremor/ladder.py       — the ladder cache: fitted segments persisted to
                          data/tremor/ladder.csv, so an hourly run refits only a
                          trailing slice of history instead of all of it — see below
tremor/saed.py          — events from the residuals and the ladder, the cooldown, the gates
tremor/persistence.py  — did the move hold at the next close
tremor/routing.py      — channel, collapse, digest slot

Computed, not delivered
tremor/si_index.py, tremor/cluster.py — the whole-basket read (see the table above)
tremor/features.py, tremor/forecast.py, tremor/threshold.py, tremor/detector.py,
tremor/market.py       — the volatility-regime detector

Bookkeeping
tremor/versioning.py   — config_version and run_version, hashes over content
tremor/journal.py      — the decision journal
tremor/export.py       — event export to JSON under a fixed schema
tremor/truth.py, tremor/evaluate.py, tremor/calibrate.py — §7 scoring
tremor/windows.py      — every window and constant, in one file

Delivery — price_monitor/
tremor_delivery.py     — renders and sends; decides nothing, routing is already stamped
weekly_digest.py       — the economic-calendar forecast, Friday, just before the note opens
health.py, notifier.py — failure reporting
twelvedata.py, coinbase.py, dukascopy.py, fxcm.py, hfdata.py — the source clients
                          tremor.backfill fetches through

data/tremor/bars/                  hourly bars, one Parquet per instrument per year   TRACKED
data/tremor/ladder.csv             the fitted rarity-ladder cache                     TRACKED
data/tremor/corporate_actions.csv  ex-dates for the funds                             TRACKED
data/tremor/sessions/              the NYSE schedule                                  TRACKED
data/tremor/vix/                   the daily VIX series                               TRACKED
data/tremor/coverage.md, evaluation.md   coverage and scoring reports                 TRACKED
data/tremor/first_valid_hour.parquet, truth_thresholds.parquet                        TRACKED
data/tremor/metrics/, residuals/, events/    gitignored — rebuilt from the bars in ~2 minutes
data/tremor/*.parquet (metrics_basket_hour, decision_log, saed_events,
  saed_block_alerts, cluster_events, cluster_event_escalations, detector_scores,
  detector_alerts, market_events, truth_labels)   gitignored — Parquet git cannot delta,
                                                    rewritten whole on every run
schema/event_export.schema.json    the event export schema, the contract for a consumer
docs/TZ_MEALS_v5.1.txt    the specification itself: what every "§4.3" in the code points at
docs/architecture.md      how Tremor works, the hourly pass in order
docs/decisions.md         why, with the measurement that settled each choice
docs/operations.md        running it, quotas, what breaks and how you would know
docs/working-agreement.md the rules an agent changing this repository works under
```

**The specification is superseded.** It is kept because the code cites its section
numbers in hundreds of comments, but where it and the measurements disagree, the
measurements win — `docs/architecture.md` (how), `docs/decisions.md` (why, with the
measurement that settled it) and `docs/operations.md` (running it) are what describe
the system now. Their predecessors — `tremor-deviations.md`, `saed-v2-plan.md`,
`tremor-v2-findings.md` — were consolidated into these three; the chronological record
of all 44 sections of the first is still in git history, not deleted.
`docs/working-agreement.md` is the shorter companion: the rules an agent changing this
repository works under, each with the measurement that earned it.

The system was called MEALS until September 2026, an acronym nobody could read as
anything but food. It is Tremor now: the ladder it reports on is a magnitude scale
measured against an instrument's own background, which is what a tremor is. The original
specification keeps the old name in its filename and its text, because renaming a document
somebody else wrote is a different thing from renaming a program.

The specification lies in the repository as `docs/TZ_MEALS_v5.1.txt`. This is not
decoration: the code refers to it hundreds of times ("§2.7", "§4.3", "§8.2"), and every
tunable number it defines is explained only there. It was written in Russian and is kept
here in translation, with the section numbering, the formulas, the field names and
every constant untouched — so every "§4.3" in the code still lands where it did. The
Russian original is not lost: it is commit `8b2fb5a`, blob `2efe49c`, SHA-256
`95382c260963375919bff2d28e02bab72958d3e490e4fef6320a993d8b3d6ba6`
(`git show 8b2fb5a:docs/TZ_MEALS_v5.1.txt`), and it stays the authority if a
translated sentence ever reads two ways. The file is DELIBERATELY kept out of
`versioning.CONFIG_INPUTS` — otherwise a typo in the text of the specification would
change `config_version` and devalue a frozen calibration just as a change to a formula
would.

Running it by hand:

```bash
export TWELVEDATA_API_KEY=...   # equities and FX
export FRED_API_KEY=...         # the VIX series only
export HFDATA_API_KEY=...       # only for deepening US-equity history before 2020

python -m tremor.backfill        # fetch new bars, merge into the store
python -m tremor.audit           # the coverage table
python -m tremor.sessions        # regenerate the NYSE schedule
python -m tremor.corporate_actions  # rebuild the ex-dividend table
python -m tremor.pipeline        # recompute per-asset metrics
python -m tremor.cross_section   # recompute basket metrics: quorum, the block factor
python -m tremor.saed            # rarity ladder (cached) + single-asset and block events
python -m tremor.cluster         # SI-Index, cluster events, the decision journal
python -m tremor.export          # export events to JSON under the schema
python -m tremor.truth           # §7 truth labels and the baseline
python -m tremor.evaluate        # score the detector against them
python -m tremor.calibrate       # §7 fit on train (writes calibration.json; never reads test)
python -m price_monitor.economic_calendar --rebuild   # rebuild the calendar archive
```

The order matters: `cross_section` works off the per-asset metrics, `saed` off the
block factor `cross_section` produced (and the ladder cache, next), `cluster` off the
basket metrics and the SAED residuals — it also writes eighteen columns *into*
`metrics_basket_hour.parquet`, so skipping it leaves that file stripped rather than
merely stale — and `export` off all of it at once.

**The rarity-ladder cache.** Each instrument's ladder — the Generalised Pareto fit that
turns a residual or a raw move into a return period — is refit monthly and does not
change between refits. `tremor.ladder` persists every fitted segment to
`data/tremor/ladder.csv`, and `tremor.saed` reads it before recomputing anything: where
the cache already covers an instrument's ladder for the hours a run needs, that
instrument is trimmed to a trailing window instead of being refit cold over its whole
history. This is the difference between an hourly run costing a slice of 23 years of
history and costing all of it. With about seventy ladders each due for a refit every
thirty days, roughly two runs a day come out fully cold and the rest are warm — and the
decision is taken for the whole run at once, not per instrument, because a run half warm
and half cold would build a cross-section out of two different amounts of history. The
cache costs nothing when it is stale or missing: that instrument just falls back to a
full refit, so the worst case is no faster, never wrong.

### Versions and reproducibility

Every decision is stamped with two versions. `config_version` is a hash of the content
of everything that affects the computation: the basket configuration and sixteen
modules with the formulas and thresholds — `tremor.versioning.CONFIG_INPUTS` is the
explicit list, deliberately hand-kept rather than hashing the whole package, so a
comment edit does not bump the version but a real change never misses it. Editing a
threshold changes the version by itself, with no manual counter that gets forgotten
exactly when it matters most.
`run_version` is a hash of `config_version` and a fingerprint of the raw data (bars,
VIX, the schedule, ex-dates, the calendar archive). Derived files are not part of the
fingerprint: the basket metrics are both an input and an output of the `cluster` run,
and including them would mean a new version on every repeat.

Per §6.3 the stamp goes on everything the run writes, not only on the journal: the
per-asset metrics, the basket metrics, the cluster events and their escalations, the
SAED events and the block alerts. A reader holding one table can then state which
version produced it, which is what §6.3 requires before events of different versions
may be compared at all.

Cluster events carry three more fields. `created_at` is the moment the row FIRST
appeared, carried across runs rather than re-taken from the clock — otherwise a rerun
over unchanged data would differ byte for byte and the idempotency of §6.2 would not
exist. `recalculated` says that late or revised data reached this row, and it is judged
on `data_fingerprint` — the third field, the hash of the raw inputs — rather than on
`run_version`. `run_version` hashes the configuration together with the data, so it
moves on a code edit too, and a flag judged on it would stand at True on every row
throughout calibration, when thresholds move on every iteration. What the code changed
is already what `config_version` is for; see `docs/decisions.md`.

SAED events carry `overlap_with_cluster` (§8.2). It is filled by the `cluster` run, not
the `saed` one: SAED runs first, so when its events are built the cluster events of this
run do not exist yet. Until then the field is NULL rather than False — under §1.2 that
is the difference between "no overlap" and "not evaluated". The span compared against is
in wall-clock hours even though the cooldown is counted in reference-calendar ones: a
crypto event can land on a Saturday, when the reference calendar has no hours at all,
and a cluster event opened on Friday is still active then.

A repeat run over unchanged data gives the same `run_version` and a byte-identical
result. And a bar sent late or corrected by the vendor changes the fingerprint, so the
recomputation gets a new version automatically.

Almost everything derived is gitignored rather than kept in the repository — the full
list is in "The pieces" above. Metrics and residuals used to be tracked; at 24
instruments they cost seconds to rebuild and a rewrite-in-full every run was cheap
enough to commit anyway, but rewritten in full twenty-four times a day at the current
basket size that stopped paying for itself, and everything past the bars is now rebuilt
from them on the run that needs it — `python -m tremor.pipeline` onward, in about two
minutes. The export format is fixed by the schema
`schema/event_export.schema.json`, and a test validates a synthetic event against it.

The archive of economic events is assembled entirely from ForexFactory's monthly pages,
from a single source and with no key. Every Friday digest reads three months back into
it — the previous one and the current one for the `actual` of released events, and the
one the coming week runs into so that week is there to be listed at all. The live weekly
feed is merged too, for the days immediately ahead. The digest is then built from the
archive over a window it states outright, which is why it can go out on a Friday: the
feed's own week boundary no longer has to be guessed. Why there is one source and what
was tried before it is above, in the calendar section.

### What actually reaches you, and the switch that stops it

Two channels feed one delivery stream, both from SAED — the only one of the three
detectors above that is delivered. They differ in what they measure, not in how
important they are — severity is a separate axis, and it is the same ladder for both.

| channel | asks | example |
|---|---|---|
| abnormal | was this move unexplained by the market and its own block | *biggest unexplained move in about a year* |
| absolute | was this simply a big move for this instrument | *biggest move in about three years* |

Both run at the block level too, not only per instrument: did a whole sector move
together, cleaned of what the rest of the market did, ranked on its own ladder the same
way. A single-instrument alert then splits the move into the two things it can be,
adding back to it exactly: the instrument's own block moving, and the instrument
itself — there is no separate basket-wide term, since the basket factor was tried and
dropped from the model (see `tremor.residuals` above).

Severity is a **return period** — how long you would ordinarily wait to see something
this large in this instrument — because "the biggest move in Bitcoin since March 2023"
needs no calibration intuition where a 1-to-100 score would. Four rungs: a fortnight,
two months, a year, three years. Fitted per instrument on its own history, so a
once-a-year move in `SHY` and one in `SOL` mean the same thing to a reader while being
wildly different percentages.

Delivery splits by urgency, not by importance:

* **Pushed at once** — both push tiers, a once-a-year move and a once-in-three-years
  one. Roughly one every eight days between them. The message is then edited in place
  at this day's close and the next day's close with how the move actually held; of
  moves still standing when their own day closed, 80% were still standing at the next
  day's close too.
* **The running digest** — everything else, 3.6 items a note on average. The note is
  *opened* Tuesday and Friday at 12:00 Israel time and then edited in place as moves
  are found, so a row appears the hour it happens rather than up to three days later.
  Telegram is silent on an edit, so this still costs exactly two interruptions a week —
  and both of them at noon: a note may only be opened in its own hour or the three
  after it, and a period that misses that window is carried into the next note.
  Tuesday covers the weekend and Monday, when crypto trades straight through and
  equities gap on the open; Friday closes the trading week. This is *separate from the
  weekly calendar digest on purpose*: that one is a forecast of what is scheduled,
  this one is a report of what happened, and reading them as one message makes both
  harder to skim.

Nothing is held back and nothing is dropped. A move that fully reverted used to take no
line at all; it is now already on the reader's phone by the time that is known, so the
line says so instead. A move folded into an earlier push is the one exception to
"everything else": the push speaks for it and lists it with its size, so it takes no row
of its own. Every row carries the settled reading, or — until the next day's close
answers it — the moment that answer is due.

`price_monitor/tremor_delivery.py` renders and sends these; it decides nothing, because
the channel and the digest slot are already stamped on each event by `tremor.routing`.
It rides the same hourly trigger as everything else rather than taking a schedule of
its own, and nothing older than 48 hours is ever sent — without that rule the first
run would deliver five years of history at once.

**The switch is `tremor_alerts_muted` in `config/config.yaml`**, currently `false`, so
messages do go out. It defaults to `true` in code — wiring the delivery and deciding to
be interrupted by it are separate acts — and it lives in the repository rather than on
the scheduler's side deliberately: *"we are deliberately silent"* is a state of the
project and has to be visible where the code is. A cron job switched off on someone
else's website looks like a breakage a month later, and there is nobody left to tell
which it was.

The NYSE schedule is built by the `exchange_calendars` library, but not on every run:
the library works as a generator, and its result lies in the repository as a table. That
way a repeat run over the same period gives the same answer even if the library has been
updated in the meantime, and the hourly monitoring does not need the calendar library at
all — it reads a ready-made CSV. The table is filled to the end of 2028; when it nears
its end, the command has to be run again.


## Running locally

```bash
pip install -r requirements-dev.txt
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export TWELVEDATA_API_KEY=...  # needed for everything except crypto, see "Quick start"
export FRED_API_KEY=...        # for the VIX series only

python -m tremor.backfill      # fetch new bars
python -m tremor.pipeline      # per-asset metrics
python -m tremor.cross_section # basket metrics
python -m tremor.saed          # single-instrument events
python -m tremor.cluster       # SI-Index and the decision journal
python -m price_monitor        # deliver whatever is due
```

The tests are run with `pytest -q`.

## Limitations

- `price-monitor.yml` has no schedule of its own. GitHub's own `schedule:` was
  measured firing roughly once every ten hours on this repository rather than once
  an hour, so an external service triggers it through the API instead. Without that
  alarm clock nothing runs, and nothing says so: the health check reports runs that
  failed, not runs that never happened. That gap is real — the trigger stopped on
  2026-09-01 and it took six days to notice.
- Twelve Data's free tier is 800 requests a day and 8 a minute. The hourly run uses
  about 293 of the day's budget (37%), which fits because the fetch skips a US equity
  or ETF when the NYSE calendar says no bar can have appeared since its newest stored
  one. FX has no such skip — it has no session table here, and its Sunday reopen is
  exactly the edge a hand-written rule would get wrong. Crypto is free; Coinbase needs
  no key. The per-minute ceiling of 8 requests, not the daily one, is what makes a run
  take minutes rather than seconds.
- The ladder cannot claim a return period longer than the history it has. A newly
  added instrument says "biggest in two months" for two years before it can say
  "biggest in three years", and says nothing at all for the first two.
