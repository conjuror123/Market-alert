# Market Alert

> **Holds** what this is, how to set it up, and how to turn it up or down.
> **Does not hold** how it works (`docs/architecture.md`), why (`docs/decisions.md`),
> or how to run it (`docs/operations.md`).
> **Keep it short.** A visitor should be able to read the whole thing.

Once an hour the bot looks at 61 instruments — equities, credit, rates, precious and
industrial metals, energy, agriculture, FX and crypto — and writes to Telegram when one
of them moves in a way that is unusual **for that instrument**.

That last part is the whole design. A 1.5% hour is nothing in SOL and enormous in short
Treasuries, so one percentage threshold shared across a basket says almost nothing. Each
instrument is measured against its own history, and the message is a date: *the biggest
move since 3 March 2020*, which needs no calibration intuition to read.

The running detector, measured over 23.3 years of hourly history, 2.9M bars, a cold pass:

| | |
|---|---|
| pushes | 1,336 — about **57 a year** |
| reached you | **94.4%** of the hours that were unmistakably large for their own instrument |
| held up | **73.9%** of pushes still standing at the next close |

The report card also prints 258 "false alarms" (events on hours below the instrument's
median move); most are overnight-gap events, which it still judges by their first hour.
Left out, the count is about 96. That rate is an output, watched rather than aimed at.

It runs entirely on GitHub Actions. Nothing extra needs hosting.

**Working on the code?** Start with `CLAUDE.md` — the pipeline order, the invariants, and
the map of these documents.

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
   | `TELEGRAM_HEALTH_CHAT_ID` | optional — health and provider failures; falls back to the product chat |
   | `TIINGO_API_KEY` | 29 US-session funds + 8 FX pairs (free: 50/hour, 1000/day) |
   | `TWELVEDATA_API_KEY` | archive, gap-fill and deepening — **not** the hourly path |
   | `FRED_API_KEY` | the VIX series only |
   | `HFDATA_API_KEY` | optional — deepening US-equity history before 2020 |

   Yahoo (15 thin ETFs) and Coinbase (9 crypto) need no key. Without a Tiingo key the
   hourly run still prices the Yahoo and Coinbase names and stays silent on the rest.
   Which providers you need is decided by `config/basket.yaml`: each instrument names its
   `provider`, and a missing key costs you those instruments and nothing else. **To add a
   provider:** a client module in `price_monitor/` exposing `fetch_full_history(...)`, its
   name in `PROVIDERS` in `tremor/basket.py`, a branch in `tremor/backfill.py`, and the
   secret in the workflows' `env:`. `source` is identity and never follows a provider change.
4. **Set up the hourly trigger — mandatory.** GitHub's own `schedule:` was measured
   firing about once every ten hours on this repository, so an external free cron
   service calls `workflow_dispatch` through the API instead. See `docs/operations.md`.

To check the setup without waiting for a signal: **Actions → Test Telegram
Notification → Run workflow**.

## Turning it up or down

**The detector is being replaced.** Production still runs the running detector described
below; its replacement, a jump detector copied from Lee & Mykland (2008), is built stage by
stage on the branch `claude/youthful-pascal-u0rx7u` (see `docs/architecture.md`). Stage 0
scores each hour against the instrument's half-year bipower volatility and words it at
3.9 / 5.5 / 7.8 / 11.0σ; over 23 years that flags about 16 hours per instrument a year
(median), before any of the stages that turn flags into messages. Its settings sit under
`detector:` in `config/basket.yaml`.

**The running detector** has two knobs in `config/basket.yaml`:

```yaml
sensitivity: 1.0      # how RARE must a move be: scales all four rungs together
min_move_sigma: 2.0   # how BIG must it be: two of the instrument's usual hours
```

`sensitivity` scales every rung at once; pushes move with roughly its 4.6th power, so a
10% turn roughly halves or doubles them. `min_move_sigma` is the size floor: the abnormal
channel asks whether a move was *unexplained*, never whether it was *large*, and without a
floor an instrument that ticked +0.03% could be reported. It is written per instrument, or
under `block_min_move_sigma` on a block's own line. Both take effect on the next hourly run
(after one cold rebuild).

Nothing records whether a message was worth reading; the knobs are turned by reading the
messages and editing the yaml.

## Where to look next

| you want | read |
|---|---|
| how a bar becomes a message | `docs/architecture.md` |
| why it is built this way | `docs/decisions.md` |
| quotas, failure modes, what is committed when | `docs/operations.md` |
| what is known and deliberately not being worked on | `docs/concerns-for-later.md` |
| which instruments, in which blocks | `config/basket.yaml` — the source of truth |
