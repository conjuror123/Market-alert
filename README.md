# Jump

An hourly Telegram bot that watches 62 market instruments (stock indices in their own
markets' hours, US funds, futures, Japan's 10-year yield, spot gold and silver, LME
metals, FX pairs, coins) and writes when one moves unusually **for itself**.

A 1.5% hour is nothing for a coin and enormous for short Treasuries, so no shared
percentage works. Each move is measured against the instrument's own last half-year,
with the jump test of Lee & Mykland (2008), and the message says how far out it is:

```
🟨 KOSPI · South Korea -5.98% · 6.5×σ
📉 Rarest hour in 2 months (then 8.2×σ)
🕐 29.07.2026 01:00 UTC · next close 129%
```

The colour is the word: ⬜ noticeable at 4.5σ, 🟨 high at 6.4σ, 🟧 major at 9σ, 🟥 extreme
at 12.7σ, each about three times rarer to reach than the one below. `high` and up arrive at once
(about 4 a week); `noticeable` goes into one weekly note (about 10 rows). Every other data
feed that carries the instrument votes on a far move: one most did not see is marked `❌`
with each feed's own move and kept out of the statistics, a tie `⚠️` and kept in. It runs entirely on
GitHub Actions; nothing else needs hosting.

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
  levels: [4.5, 6.4, 9.0, 12.7]   # noticeable, high, major, extreme, in sigmas
```

The bottom level sets how much you hear: 4.5 gives about 3.8 pushes and 10.5 note rows a
week, 3.9 about 7.0 and 15.8, 6 about 1.4 and 3.3 (the stage report's count over the year
to 2026-10-05). Which words push is `PUSH_TIERS` in `jump/routing.py`. To silence the
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
| fetching bars, and the sources' vote | `jump/backfill.py`, `jump/verify.py` |
| what goes to Telegram | `price_monitor/jump_delivery.py` |
| the hourly workflow | `.github/workflows/price-monitor.yml` |
| rates by word and block, the biggest hours | `tools/stage_report.py` |
| the dashboard: volume, the votes taken again on the voters' bars, and the archive, measured on every code change | `.github/workflows/measure.yml` (`tools/measure.py`, `tools/dashboard.py`), published at https://conjuror123.github.io/Market-alert/ |
