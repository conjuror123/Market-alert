# Tremor — orientation for an agent

> **Holds** what an agent needs in the first minute: the command order, the invariants,
> the map, and how to run the tests. It is loaded automatically, so it stays short.
> **Does not hold** anything explained elsewhere. It is the canonical copy of the hourly
> pass and of local setup; everything else here is a pointer.
> **Add to it only** what would cause a wrong change if it were not known immediately.

An hourly Telegram bot. It watches 173 market instruments and writes when one moves
unusually **for itself**, measured against its own history rather than a shared
percentage. It runs entirely on GitHub Actions.

**This branch is the replacement detector.** A jump detector copied from Lee & Mykland
(2008), built stage by stage in `tremor/jumps.py` on `claude/youthful-pascal-u0rx7u`, as it
will run live. Production still runs the previous detector from
`claude/price-spike-monitoring-app-yyg2jg` until the switch; its code is not on this branch
— read it there when a stage needs a piece of it. Each stage is reviewed before the next;
`docs/architecture.md` has the stage list and `docs/decisions.md` the reasons.

**Where it posts: a PUBLIC Telegram channel, not a private chat.** Anyone can join it. The
bot is an administrator there. A bot can edit its own messages at any age, and as a
channel admin with "Delete messages" (`can_delete_messages`) it can delete any message
there — the Bot API's 48-hour delete limit does not bind it. Never reason, write or log as
if this were a private chat. Health and provider failures never go there: without
`TELEGRAM_HEALTH_CHAT_ID` they are only logged.

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
tremor.jumps      score, words, 24-hour events, channels    <- the product
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
4. **An event is 24 real hours from its first move being found** (`jumps.event_starts`),
   for every instrument — not a trading day, not a count of candles. Its word is its
   rarest reading's, its numbers its biggest reading's. A move found after the 24 hours
   opens the next event; one found after the week's note opened opens a new event even
   inside them. Events already on the channel hold their 24 hours (delivery's anchors).
5. **One note a week, curated for that week and never after.** It opens at the first run
   after the week's last NYSE close (normally Friday 16:05 New York; `routing.digest_slot`),
   right after the calendar's own message. That run first finishes the old week — its
   moves' checks at that close land — then opens the new note; the closing hour is found
   in it and goes into the new week. A move belongs to the note open when it is
   **found**; for that week every run brings every message in line with the events table
   (`tremor_delivery` "the week"). Anything of an earlier week is history and is never
   touched: at the next note only the old week's pings are deleted. **Inside its 24
   hours** an event that turns rarer is deleted and goes out again at the new word, and
   rings; one that turns milder is edited (a push that falls to `noticeable` shows ⬜, a
   `noticeable` that falls away is deleted). **After them** it changes only by a fix,
   silently: rarer or milder is an edit (a row turning `high` leaves the note and its
   ping is edited into the push), gone is gone for good. A changed event carries its
   story on one line; a clean one says nothing. A ping lives exactly as long as its row.
   Every move — a coin's and a pair's too — is checked at the first NYSE close after it
   was found (`jumps.held_at_close`): its time line counts down, then says how much held.
6. **A detector update restarts the week.** When `jumps.detector_version()` changes, every
   push and ping of the week is deleted; the note (and the calendar) stay, and the week
   continues with what is found from that run on. Nothing found before the update rings.
7. **A reading is judged only once it can be.** The run fires at :05 and stores the hour
   it is standing in — a few per cent of its volume — so `jumps.ended` scores an hour
   only after it ends, a fund's gap with its first bar, once that bar has ended, and a
   currency pair's weekend gap at its open. The stored bars heal on the next fetch, so
   the metrics must too: `extend_asset_metrics` re-scores its last `RECOMPUTE_TAIL_BARS`
   rows instead of trusting them.
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

To exercise the real pipeline you need `TIINGO_API_KEY` and `SIFTING_API_KEY` (hourly
bars: funds and FX), plus
`TWELVEDATA_API_KEY` and `FRED_API_KEY` for archive work. Derived data under
`data/tremor/metrics/` and `jumps.parquet` is gitignored and rebuilds from the committed
bars in about fifteen seconds.

