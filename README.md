# Market Alert

> **Holds** what this is, how to set it up, and how to turn it up or down.
> **Does not hold** how it works (`docs/architecture.md`), why (`docs/decisions.md`),
> or how to run it (`docs/operations.md`).
> **Keep it short.** A visitor should be able to read the whole thing.

Once an hour the bot looks at 173 instruments — equities, credit, rates, precious and
industrial metals, energy, agriculture, FX and crypto — and writes to Telegram when one
of them moves in a way that is unusual **for that instrument**.

That last part is the whole design. A 1.5% hour is nothing in SOL and enormous in short
Treasuries, so one percentage threshold shared across a basket says almost nothing. Each
move is measured against the instrument's own last half-year — the jump test of Lee &
Mykland (2008) — and the message says how big it was in those terms:

```
🟨 LTC-USD · Litecoin +5.76% · 11.0×σ
🕐 24.09.2026 02:00 UTC
```

The square's colour is the word: ⬜ noticeable at 6σ, 🟨 high at 8.5σ, 🟧 major at 12σ,
🟥 extreme at 17σ — each about three times rarer than the one below. For one instrument
that is a noticeable move about once a season, a high one twice a year, a major one every
couple of years and an extreme one every four. An instrument's event is the 24 hours from
its first move, worded by its rarest hour. `high` and up arrive at once, about 4 a week for
today's basket; `noticeable` goes into one weekly note, about 11 rows a week, each with a
ping line (the year to 2026-10-01). What one hourly run finds goes out together — one
message for its pushes, one for its pings, biggest first — about 9 messages a week, from
about 4 in a calm year to 18 in 2008. The detector is built in stages (`docs/architecture.md`); this branch is it as it
will run live, and production runs the previous detector until the switch.

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
   | `TELEGRAM_HEALTH_CHAT_ID` | optional — health and provider failures; without it they are only logged, never sent to the public channel |
   | `TIINGO_API_KEY` | 27 funds (free: 50/hour, 1000/day) |
   | `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` | 30 funds live (free IEX feed); history from 2016 (consolidated tape) |
   | `SIFTING_API_KEY` | the 17 FX pairs (free: 10,000/month) |
   | `TWELVEDATA_API_KEY` | 8 thin funds every hour (free: 800/day, 8/minute); archive, gap-fill and deepening |
   | `FRED_API_KEY` | the VIX series only |
   | `HFDATA_API_KEY` | optional — deepening US-equity history before 2020 |

   Yahoo (34 funds, 4 futures), Sina Finance (33 funds; LME tin, nickel and aluminium),
   Google Finance (TUR) and Binance (16 coins) need no key. Without a key the hourly run still prices
   the other providers' names and stays silent on that one's.
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

Three settings, under `detector:` in `config/basket.yaml`:

```yaml
detector:
  window_days: 182.6       # how far back "usual" reaches, the same calendar span for all
  noticeable_sigma: 6.0    # the lowest word; every word above is `step` times bigger
  step: 1.414
```

`noticeable_sigma` is the one that sets how much you hear: 6 is about 9 messages a week, 5
about 15 and 3.9 about 29 (delivery replayed over the year to 2026-10-01,
`docs/decisions.md`). Which words push is `PUSH_TIERS` in `tremor/routing.py`. All take
effect on the next hourly run.

Nothing records whether a message was worth reading; the settings are turned by reading
the messages and editing the yaml.

## Where to look next

| you want | read |
|---|---|
| how a bar becomes a message | `docs/architecture.md` |
| why it is built this way | `docs/decisions.md` |
| quotas, failure modes, what is committed when | `docs/operations.md` |
| what is known and deliberately not being worked on | `docs/concerns-for-later.md` |
| which instruments, in which blocks | `config/basket.yaml` — the source of truth |
