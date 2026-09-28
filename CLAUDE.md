# Tremor — orientation for an agent

> **Holds** what an agent needs in the first minute: the command order, the invariants,
> the map, and how to run the tests. It is loaded automatically, so it stays short.
> **Does not hold** anything explained elsewhere. It is the canonical copy of the hourly
> pass and of local setup; everything else here is a pointer.
> **Add to it only** what would cause a wrong change if it were not known immediately.

An hourly Telegram bot. It watches 61 market instruments and writes when one moves
unusually **for itself**, measured against its own history rather than a shared
percentage. It runs entirely on GitHub Actions.

**This branch is the replacement detector.** A jump detector copied from Lee & Mykland
(2008), built stage by stage in `tremor/jumps.py` on `claude/youthful-pascal-u0rx7u`, as it
will run live. Production still runs the previous detector from
`claude/price-spike-monitoring-app-yyg2jg` until the switch; its code is not on this branch
— read it there when a stage needs a piece of it. Each stage is reviewed before the next;
`docs/architecture.md` has the stage list and `docs/decisions.md` the reasons.

Read this file first, then the one doc that covers your task:

| you need | read |
|---|---|
| what it does, how to set it up, how to tune it | `README.md` |
| how a bar becomes a message, module by module | `docs/architecture.md` |
| why it is built this way, and what not to re-litigate | `docs/decisions.md` |
| quotas, failure modes, what is committed when | `docs/operations.md` |
| the rules you work under here | `docs/working-agreement.md` |
| what is known, open, and deliberately not being worked on | `docs/concerns-for-later.md` |
| which instruments, in which blocks | `config/basket.yaml` — the source of truth |

## The hourly pass

Four commands. **The order is load-bearing.**

```
tremor.backfill   fetch new bars into data/tremor/bars/
tremor.pipeline   per-instrument metrics: the move and the gap
tremor.jumps      score, words, one event a day, channels   <- the product
price_monitor     deliver what is due to Telegram
```

- `jumps` reads what `pipeline` wrote and rescores the whole history every run.
- `price_monitor` is last: delivery reads `jumps.parquet` off disk.

## Invariants

Break one of these and the system is wrong rather than merely broken.

1. **`source` is identity, `provider` is who is asked.** In `config/basket.yaml`,
   `asset_id` and the file on disk are built from `source`. Editing it to follow a
   provider change orphans every stored bar and every recorded verdict. `provider`
   changes freely.
2. **Rolling windows end before the bar being judged.** A full-sample fit labels a 2016
   move knowing 2020 is coming, and the backtest then flatters a system nobody can run.
3. **Everything internal is UTC seconds, named `hour_utc`.** Local time appears where a
   day is a local thing — a fund's trading day and session, in New York — resolved
   through `ZoneInfo`. The weekly note's slot is UTC.
4. **One event per instrument per day, unless the day grows.** Enforced in
   `jumps.one_a_day`, not by filtering afterwards: a later reading that day is kept only if
   its word is rarer than every one kept before it. The New York date for the funds, the
   UTC date for currency pairs and crypto.
5. **A ping exists only while the note beneath it shows its row.** `pending_pings` and
   `restyle_pings` both bound on the open note's window; they must not diverge.
6. **A note interrupts only while its period is open.** Every note inside
   `DIGEST_TRACK_HOURS` is re-rendered from the events table each run, so it stays
   correctable — but an edit is silent and a new part is a notification. Past
   `DIGEST_GROW_AFTER_CLOSE_HOURS` beyond its window, a note may be corrected and may
   not grow. One note a week, Saturday, right after the calendar's own message.
7. **An hour is scored from the bar it ends with, not the bar it starts with.** The run
   fires at :05 and stores the hour it is standing in — a few per cent of its volume.
   The bars heal on the next fetch, so the metrics must too: `extend_asset_metrics`
   re-scores its last `RECOMPUTE_TAIL_BARS` rows instead of trusting them.
8. **Tables are written through a temp file and `os.replace`** (`tremor/atomic.py`), so a
   killed run cannot truncate one in place.
9. **Secrets never enter the repository.** The repo is public. `config.yaml` may name a
   secret; it may never hold one.
10. **A change to a formula moves `config_version`**, and `pipeline` then rebuilds cold
    instead of extending. The first run after such a change is slow by design. A comment
    does not: the inputs are hashed as parsed code, docstrings stripped, so rewriting
    prose cannot force a rebuild or move an event's stamp.

## Working locally

```bash
pip install -r requirements-dev.txt
pytest -q                     # ~580 tests, about a minute
```

Run the tests alone — several load large parquet files, and concurrent runs thrash.

To exercise the real pipeline you need `TIINGO_API_KEY` (hourly bars), plus
`TWELVEDATA_API_KEY` and `FRED_API_KEY` for archive work. Derived data under
`data/tremor/metrics/` and `jumps.parquet` is gitignored and rebuilds from the committed
bars in about fifteen seconds.

