# Jump

An hourly Telegram bot that watches 173 market instruments (US funds across equity,
credit, rates and commodities, futures, LME metals, FX pairs, coins) and writes when one
moves unusually **for itself**.

A 1.5% hour is nothing for a coin and enormous for short Treasuries, so no shared
percentage works. Each move is measured against the instrument's own last half-year,
with the jump test of Lee & Mykland (2008), and the message says how far out it is:

```
🟨 LTC/USDT · Litecoin +5.77% · 11.1×σ
📈 Rarest hour in 16 days (then 11.5×σ)
🕐 24.09.2026 02:00 UTC · next close 242%
```

The colour is the word: ⬜ noticeable at 6σ, 🟨 high at 8.5σ, 🟧 major at 12σ, 🟥 extreme
at 17σ, each about three times rarer than the one below. `high` and up arrive at once
(about 4 a week); `noticeable` goes into one weekly note (about 11 rows). A move another
data feed did not see is marked `⚠️ unconfirmed` and kept out of the statistics. It runs
entirely on GitHub Actions; nothing else needs hosting.

## Quick start

1. Create a bot with [@BotFather](https://t.me/BotFather), and a channel with the bot as
   administrator (post, edit and delete messages).
2. Add the secrets under **Settings → Secrets and variables → Actions**:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and the provider keys listed in
   `docs/manual.md`, "Setup and tests". A missing provider key costs only that
   provider's instruments.
3. Point an external cron service at the `price-monitor.yml` workflow's `workflow_dispatch`
   at :05 every hour (`docs/manual.md`, "Running it"). GitHub's own `schedule:` fires too
   unreliably to use.

Locally: `pip install -r requirements-dev.txt`, then `pytest -q`.

## Turning it up or down

Under `detector:` in `config/basket.yaml`:

```yaml
detector:
  window_days: 182.6              # how far back "usual" reaches
  levels: [6.0, 8.5, 12.0, 17.0]  # noticeable, high, major, extreme, in sigmas
```

The bottom level sets how much you hear: 6 gives about 9 messages a week, 5 about 15,
3.9 about 29. Which words push is `PUSH_TIERS` in `jump/routing.py`. To silence the
channel without stopping the bot, set `jump_alerts_muted: true` in
`config/config.yaml`.

## Where to find what

| you want | read |
|---|---|
| how the bot works, how to run it, its rules and known limitations | `docs/manual.md` |
| why it is built this way; rejected options; open questions | `docs/decisions.md` |
| how an agent works on this repository | `CLAUDE.md` |
| which instruments, in which blocks, from which provider | `config/basket.yaml` (the source of truth) |
| run settings: health alerts, the mute, the calendar | `config/config.yaml` |
| how much history each instrument has | `data/jump/coverage.md` |
| the detector | `jump/jumps.py` |
| fetching bars, and the second-source check | `jump/backfill.py`, `jump/verify.py` |
| what goes to Telegram | `price_monitor/jump_delivery.py` |
| the hourly workflow | `.github/workflows/price-monitor.yml` |
| rates by word and block, the biggest hours | `tools/stage_report.py` |
