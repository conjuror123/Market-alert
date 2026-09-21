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

Measured over 23.3 years of hourly history, 2.9M bars, a cold pass on the current code:

| | |
|---|---|
| pushes | 1,352 — about **58 a year**, on 37 interrupted days |
| reached you | **94.4%** of the 216 hours that were unmistakably large for their own instrument; 12 silent |
| false alarms | **0.007%** — 85 of 1.17M hours below their instrument's median move |
| held up | **74.3%** of pushes still standing at the next close |

That rate is an OUTPUT, watched rather than aimed at. Nothing caps it and the rungs are
not tuned against it: they are tuned against what each word should mean for a single
instrument, so the yearly total is whatever 61 instruments of that sensitivity happen to
produce. Widening the basket raises it, and that is not a fault.

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
4. **Set up the hourly trigger — mandatory.** GitHub's own `schedule:` was measured
   firing about once every ten hours on this repository, so an external free cron
   service calls `workflow_dispatch` through the API instead. See `docs/operations.md`.

To check the setup without waiting for a signal: **Actions → Test Telegram
Notification → Run workflow**.

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

**Neither number is guessable from the data**, and nothing in the system tries. Both are
edited by hand in `config/basket.yaml` and take effect on the next hourly run.
`min_move_sigma` is written per instrument, `block_min_move_sigma` on a block's own line
without copying onto its members.

There was a Telegram command for this — `/floor BKLN 2.5` — and a recorder for verdicts
on messages that were or were not worth reading. Both are gone. In the project's life the
command never changed a number and the recorder held one row, while between them they
cost two of the six steps in the hourly pass. **So nothing now records whether a message
was worth reading.** That is a real thing given up: a reader can point at a message that
arrived and should not have, and cannot point at one that never came, so judgement was
the only evidence the loud side was too loud. The knobs are turned by reading the messages
and editing the yaml.

## Where to look next

| you want | read |
|---|---|
| how a bar becomes a message | `docs/architecture.md` |
| why it is built this way | `docs/decisions.md` |
| quotas, failure modes, what is committed when | `docs/operations.md` |
| what is known and deliberately not being worked on | `docs/concerns-for-later.md` |
| which instruments, in which blocks | `config/basket.yaml` — the source of truth |
