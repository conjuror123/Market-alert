# Tremor — orientation for an agent

An hourly Telegram bot. It watches 61 market instruments and writes when one moves
unusually **for itself**, measured against its own history rather than a shared
percentage. It runs entirely on GitHub Actions.

Read this file first, then the one doc that covers your task:

| you need | read |
|---|---|
| what it does, how to run it, how to tune it | `README.md` |
| how a bar becomes a message, module by module | `docs/architecture.md` |
| why it is built this way, and what not to re-litigate | `docs/decisions.md` |
| quotas, failure modes, what is committed when | `docs/operations.md` |
| the rules you work under here | `docs/working-agreement.md` |

## The hourly pass

Six commands. **The order is load-bearing.**

```
price_monitor.floor          apply any /floor command before anything is scored
tremor.backfill              fetch new bars into data/tremor/bars/
tremor.pipeline              per-instrument metrics
tremor.saed                  residuals, ladder, events, routing      <- the product
price_monitor.floor --reply  answer /floor now this run has scored it
price_monitor                deliver what is due to Telegram
```

- `saed` reads what `pipeline` wrote and builds its cross-section in memory.
- `floor` runs twice because the yaml edit must land *before* `saed` scores the hour,
  and the reply must wait *after* it — the events table it counts from is gitignored,
  so a fresh runner has nothing to count until this run writes it.
- `price_monitor` is last: delivery reads `saed_events.parquet` off disk.

## Invariants

Break one of these and the system is wrong rather than merely broken.

1. **`source` is identity, `provider` is who is asked.** In `config/basket.yaml`,
   `asset_id` and the file on disk are built from `source`. Editing it to follow a
   provider change orphans every stored bar and every recorded verdict. `provider`
   changes freely.
2. **Rolling windows end before the bar being judged.** A full-sample fit labels a 2016
   move knowing 2020 is coming, and the backtest then flatters a system nobody can run.
3. **Everything internal is UTC seconds, named `hour_utc`.** Local time appears in one
   place only — the digest slot, because the reader reads it locally — resolved through
   `ZoneInfo`.
4. **One event per instrument per trading day.** Enforced in `saed`, not by filtering
   afterwards. End of session for the funds, end of the UTC day for crypto.
5. **A ping exists only while the note beneath it shows its row.** `pending_pings` and
   `restyle_pings` both bound on the open note's window; they must not diverge.
6. **A note interrupts only while its period is open.** Every note inside
   `DIGEST_TRACK_HOURS` is re-rendered from the events table each run, so it stays
   correctable — but an edit is silent and a new part is a notification. Past
   `DIGEST_GROW_AFTER_CLOSE_HOURS` beyond its window, a note may be corrected and may
   not grow. Two interruptions a week, and no third.
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
pytest -q                     # ~820 tests, about four minutes
```

Run the tests alone — several load large parquet files, and concurrent runs thrash.

To exercise the real pipeline you need `TIINGO_API_KEY` (hourly bars), plus
`TWELVEDATA_API_KEY` and `FRED_API_KEY` for archive work. Derived data under
`data/tremor/metrics/`, `residuals/` and `saed_events.parquet` is gitignored and rebuilds
from the committed bars in about two minutes.

