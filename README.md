# Market Alert

Once an hour the bot looks at a basket of twenty-four instruments — equities,
credit, rates, commodities, FX and crypto — and writes to Telegram when one of
them moves in a way that is unusual **for that instrument**.

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
4. Currency pairs go through a separate provider, [Twelve Data](https://twelvedata.com/) —
   register there (email only, no card) and add the key alongside the others under the
   name `TWELVEDATA_API_KEY`. Without it the hourly monitoring keeps working on the
   remaining assets, but stays silent on all eight currency pairs.
5. Set up the hourly trigger — this is mandatory, otherwise the bot will never start
   on its own at all, only manually through Run workflow. GitHub Actions' native
   schedule is unreliable on repositories like this one (it can genuinely fire once
   every 5-10 hours), so instead use an external free alarm clock, see "A reliable
   hourly trigger" below (~10 minutes of setup).

To check the setup without waiting for a real signal: **Actions → Test Telegram
Notification → Run workflow** — it sends a test message and nothing else.

## Which instruments are tracked

Twenty-four, in five blocks. The blocks are not decoration: the detector removes
what the whole market did before asking whether one instrument moved on its own,
and a block is the group it is compared against first.

| Block | Instruments |
|---|---|
| FX | Australian dollar / dollar, Dollar / Canadian dollar, Dollar / franc, Dollar / offshore yuan, Dollar / yen, Dollar / yuan, Euro / dollar, New Zealand dollar / dollar, Pound / dollar |
| commodities | Broad commodity basket, Gold, Silver, WTI crude oil |
| crypto | Bitcoin, Ethereum, Solana |
| equity | Nasdaq 100, Russell 2000 (small caps), S&P 500, US financial sector |
| rates | High-yield bonds (credit risk), Treasuries 1-3 years, Treasuries 20+ years, Treasuries 7-10 years |

The list lives in `config/basket.yaml`, one entry per instrument, with its source,
tick size and trading calendar. There are no per-instrument thresholds to set — the
ladder is fitted from each instrument's own history, which is the point.

**Adding one** means adding the entry and running `python -m tremor.backfill` to
acquire its history. Two things to know before you do. Its ladder stays empty for
two years of bars and can only claim a return period as long as the history it has,
so a new instrument is quiet at first and then conservative for a while. And the
Twelve Data free tier is 8 requests a minute and 800 a day, which the current basket
already uses about 485 of.

## Tremor — how it decides

Tremor is built to a separate technical specification, version 5.1. It replaced an
earlier detector — sixteen assets, one shared pair of z-score thresholds, no notion
of how rare a move was — rather than refining it. Two things run: a cross-sectional
analysis of the whole basket (the Composite Sensation Index), and a module for
single-instrument moves cleaned of the common market factor.

The system was called MEALS until September 2026. See "Versions and
reproducibility" below for why the original specification keeps that name.

The detector it replaced is gone — deleted, not disabled. What it did badly is on the
record in `docs/tremor-deviations.md`: one threshold shared by every instrument, so a
1.5% hour meant the same thing in short Treasuries as in Solana; nothing to say how
rare a move was once it fired; and no way to tell an instrument moving on its own from
the whole market moving together.

The pieces (phases 0–6: data, computation, events, journal and export):

```
config/basket.yaml   — basket composition: 21 assets, five blocks, tiers, price steps
tremor/basket.py      — loading the basket, weights by the equal-weight rule
tremor/bars.py        — the hourly bar store (Parquet) and assembling the hourly grid
tremor/sessions.py    — the NYSE session calendar and the basket's reference calendar
tremor/windows.py     — the registry of windows and constants, three incompatible time units
tremor/quality.py     — the bar quality gate and whether an hour belongs to a session
tremor/returns.py     — returns, the gap channel of the first bar of a session, winsorization
tremor/zscore.py      — the out-of-sample EWMA Z-score and adaptive Q95/Q99 thresholds
tremor/volume.py      — a robust volume profile by local exchange hour
tremor/pipeline.py    — per-asset metrics, the whole phase 1-2 chain in one pass
tremor/cross_section.py — the hour's quorum, M_t, basket dispersion, PCA, single-factorness
tremor/residuals.py   — regression on the basket factor and the block factor, the residual series
tremor/saed.py        — single-asset events from the residuals and block alerts
tremor/calendar_multiplier.py — an hour's importance multiplier from the economic calendar
tremor/vix.py         — the stress multiplier from the daily VIX series
tremor/si_index.py    — base points and the Composite Sensation Index
tremor/cluster.py     — cluster events: the gate, the cooldown, escalations
tremor/versioning.py  — config_version and run_version, run idempotency
tremor/journal.py     — the decision journal and the trigger readiness table
tremor/export.py      — exporting a cluster event to JSON under a fixed schema
tremor/corporate_actions.py — ex-dividend dates derived from the quotes
tremor/fred.py        — the daily VIX series from FRED and the moment it becomes available
tremor/backfill.py    — the one-off load of history from 2021
tremor/audit.py       — the data coverage table, required by §2.1 of the spec
tremor/truth.py       — the §7 yardstick: truth labels by block and the SPY baseline
tremor/evaluate.py    — precision, recall, F1, lead time and the §7 diagnostics
tremor/calibrate.py   — the §7 fit on train, folded and frozen before test is read

data/tremor/bars/     — hourly bars per instrument
data/tremor/vix/      — the daily VIX series
data/tremor/sessions/ — the NYSE schedule: trading days and half sessions
data/tremor/corporate_actions.csv — ex-dates for the funds
data/tremor/metrics/  — per-asset metrics (metrics_asset_hour)
data/tremor/metrics_basket_hour.parquet — basket metrics by hour
data/tremor/saed_events.parquet — single-asset events
data/tremor/saed_block_alerts.parquet — block alerts
data/tremor/cluster_events.parquet — cluster events
data/tremor/cluster_event_escalations.parquet — escalations inside events
data/tremor/residuals/ — residual series per instrument (not in the repository, see below)
data/tremor/decision_log.parquet — the decision journal: magnitude, threshold, outcome, versions
data/tremor/first_valid_hour.parquet — from which hour a trigger can be trusted
data/tremor/truth_labels.parquet — §7 labels: was the next 24h significant, and the baseline
data/tremor/truth_thresholds.parquet — the per-block Q99 those labels rest on, taken on train
data/tremor/evaluation.md — the scoring report: the detector against the §7 baselines
data/tremor/calibration.json — the search: what was tried, what was chosen, how it held up
data/tremor/frozen.json — the frozen config_version and the train result behind it
data/tremor/events/   — event export in JSON (not in the repository, see below)
data/tremor/coverage.md — the coverage table
schema/event_export.schema.json — the event export schema, the contract for a consumer
docs/TZ_MEALS_v5.1.txt — the specification itself: what every "§4.3" in the code points at
docs/tremor-deviations.md — departures from the spec, each with its reason
```

**The specification is superseded.** It is kept because the code cites its section
numbers in hundreds of comments, but where it and the measurements disagree, the
measurements win — `docs/tremor-deviations.md`, `docs/saed-v2-plan.md` and
`docs/tremor-v2-findings.md` are what describe the system now.
`docs/working-agreement.md` is the shorter companion: the rules an agent changing this
repository works under, each with the measurement that earned it.

The system was called MEALS until September 2026, an acronym nobody could read as
anything but food. It is Tremor now: the ladder it reports on is a magnitude scale
measured against an instrument's own background, which is what a tremor is. The original
specification keeps the old name in its filename and its text, because renaming a document
somebody else wrote is a different thing from renaming a program.

The specification lies in the repository as `docs/TZ_MEALS_v5.1.txt`. This is not
decoration: the code refers to it hundreds of times ("§2.7", "§4.3", "§8.2"), and all
sixty tunable numbers are explained only there. It was written in Russian and is kept
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
export TWELVEDATA_API_KEY=...   # the same key as the current monitoring uses
export FRED_API_KEY=...         # new, for the VIX series only
python -m tremor.backfill        # the one-off load of history
python -m tremor.audit           # the coverage table
python -m tremor.sessions        # regenerate the NYSE schedule
python -m tremor.corporate_actions  # rebuild the ex-dividend table
python -m tremor.pipeline        # recompute per-asset metrics
python -m tremor.cross_section   # recompute basket metrics
python -m tremor.saed            # recompute single-asset events
python -m tremor.cluster         # SI-Index, cluster events, the decision journal
python -m tremor.export          # export events to JSON under the schema
python -m tremor.truth           # §7 truth labels and the baseline
python -m tremor.evaluate        # score the detector against them
python -m tremor.calibrate       # §7 fit on train (writes calibration.json; never reads test)
python -m price_monitor.economic_calendar --rebuild   # rebuild the calendar archive
```

The order matters: `cross_section` works off the per-asset metrics, `saed` off the
basket factor, `cluster` off the basket metrics and the SAED residuals, and `export`
off all of it at once.

### Versions and reproducibility

Every decision is stamped with two versions. `config_version` is a hash of the content
of everything that affects the computation: the basket configuration and the thirteen
modules with the formulas and thresholds. Editing a threshold changes the version by
itself, with no manual counter that gets forgotten exactly when it matters most.
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
is already what `config_version` is for; see `docs/tremor-deviations.md` §17.

SAED events carry `overlap_with_cluster` (§8.2). It is filled by the `cluster` run, not
the `saed` one: SAED runs first, so when its events are built the cluster events of this
run do not exist yet. Until then the field is NULL rather than False — under §1.2 that
is the difference between "no overlap" and "not evaluated". The span compared against is
in wall-clock hours even though the cooldown is counted in reference-calendar ones: a
crypto event can land on a Saturday, when the reference calendar has no hours at all,
and a cluster event opened on Friday is still active then. On the current history 1,494
of 2,478 single-asset events fall inside an active cluster event.

Two properties follow. A repeat run over unchanged data gives the same `run_version` and
a byte-identical result — verified over the whole history: 188 cluster events, 324,950
journal rows and 188 export files match between two runs. And a bar sent late or
corrected by the vendor changes the fingerprint, so the recomputation gets a new version
automatically.

Two directories are not kept in the repository: the event export `data/tremor/events/`
(9.7 MB of JSON, rewritten in full on every run — `run_version` is in every file) and
the residual series `data/tremor/residuals/` (32 MB). Both are fully derived from the
metrics and the code and are restored in seconds: `python -m tremor.saed` and
`python -m tremor.export`. The per-asset metrics stay in the repository — they depend
only on the bars and cost a full `pipeline` run. The export format is fixed by the
schema `schema/event_export.schema.json`, and a test validates against it both a
synthetic event and all 188 real ones.

The archive of economic events is assembled entirely from ForexFactory's monthly pages,
from a single source and with no key. The live weekly feed extends it forward on every
Saturday digest, and the actual for released events is read back from the same place
month by month. Why there is one source and what was tried before it is above, in the
calendar section, and in `docs/tremor-deviations.md`.

### What actually reaches you, and the switch that stops it

Three channels feed one delivery stream. They differ in what they measure, not in how
important they are — severity is a separate axis, and it is the same ladder for all
three.

| channel | asks | example |
|---|---|---|
| abnormal | was this move unexplained by the market | *biggest unexplained move in about a year* |
| absolute | was this simply a big move for this instrument | *biggest move in about three years* |
| market | was the market as a whole disorderly | *most disorderly hour in about two months* |

Severity is a **return period** — how long you would ordinarily wait to see something
this large in this instrument — because "the biggest move in Bitcoin since March 2023"
needs no calibration intuition where a 1-to-100 score would. Four rungs: a fortnight,
two months, a year, three years. Fitted per instrument on its own history, so a
once-a-year move in `SHY` and one in `SOL` mean the same thing to a reader while being
wildly different percentages.

Delivery splits by urgency, not by importance:

* **Pushed at once** — the rarest tier. Roughly one every three to four weeks.
* **Pushed after six bars** — a once-a-year move, and only if it is still standing.
  Of the events still standing at six bars, 72% were still standing at twenty-four,
  against 32% of those that had already given it back.
* **Tuesday and Friday digest** — everything else that held, about two items a note.
  Tuesday covers the weekend and Monday, when crypto trades straight through and
  equities gap on the open; Friday closes the trading week. This is *separate from the
  Saturday calendar digest on purpose*: that one is a forecast of what is scheduled,
  this one is a report of what happened, and reading them as one message makes both
  harder to skim.
* **Dropped** — the move reverted. Not a failure of the detector: it correctly found an
  unusual move, and then the move gave itself back. About two in five.

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
export TWELVEDATA_API_KEY=...  # needed for the currency pairs, see "Quick start"
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
- Twelve Data's free tier is 800 requests a day and 8 a minute. The hourly run asks
  for about 293 and `price_monitor` for 192, which fits because the fetch skips a US
  equity when the NYSE calendar says no bar can have appeared. The other half of that
  duplication — eight FX pairs fetched twice — is still there.
- The ladder cannot claim a return period longer than the history it has. A newly
  added instrument says "biggest in two months" for two years before it can say
  "biggest in three years", and says nothing at all for the first two.
