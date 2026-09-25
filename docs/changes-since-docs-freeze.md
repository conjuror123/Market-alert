# Changes since the docs freeze

> **Holds** what changed since the other documents started being edited by hand (22–23 September
> 2026), and where each change belongs. Describes the current state only; delete an entry once
> it is folded in.

## The overnight gap

A second reading beside the hours: the move from the previous session's last close to the next
session's first price, covering the whole night for a fund and the whole weekend for a currency.
Crypto has none. `tremor/gaps.py`.

- **Its own path.** Its own series (one row per session), yardstick (daily memory, as the VIX),
  block comparison, rungs, "biggest since" records and events. Nothing hourly reads it. Always
  computed over the full history, never warm (about 8 s).
- **Kind of close.** Judged against the usual gap after the same kind of close: weekend or
  weeknight. A midweek holiday counts as a weeknight. A weekend gap is typically 1.17× a
  weeknight's (from 0.88× for XLP to 1.81× for UNG), learned per fund over 500 sessions. For FX
  every gap is a weekend one.
- **One event per day.** When the gap and an hour both fire on the same day, the day is
  described by the rarer of the two (at the same rung, whichever went further past it). The
  event keeps the gap's identity. `saed.merge_days`.
- **Unscored** when the day's dividends aren't confirmed, on a split, when the previous stored
  bar isn't the last one before the close (NYSE calendar for funds, 50 h for FX), and on the
  first bar of the record.
- **Dividend check.** On the first run after 09:30 New York, `tremor.backfill` asks Yahoo for
  payouts. New ones go to `corporate_actions.csv`; `dividend_checks.csv` records how far each
  fund is checked. Both files are committed every run. When a fund is 5+ days behind, one line a
  day goes to the health chat.
- **Messages:** "at the open", "at the open after the weekend", "at the weekly open" (FX);
  "usual overnight / weekend gap"; "block opening", "biggest gaps: …". New event column:
  `gap_kind`.
- `tremor/gaps.py` is part of `config_version`.

**Belongs in:** `architecture.md` "1. The return" (draft below); `decisions.md` (its "gap
channel deleted" entry is now wrong; why holidays count as weeknights); `operations.md` (Yahoo
check, the two committed files, health line); `CLAUDE.md` invariant 10 (module list) and
invariant 4 (now also enforced in `merge_days`).

## Time of day for a fund's hour

The "unusual compared with its block" check divides each hour of a US fund by that hour's usual
size relative to all hours, learned per fund over 500 sessions (`residuals.hour_scale`). The
09:00 crossing rate fell from 0.61% to 0.35%. The opening still crosses about 2× as often,
because of more extreme outliers; that's for the rung re-cut. FX is unchanged, because its busy
hour is the news.

**Belongs in:** `architecture.md` (abnormal channel); `decisions.md` (why 500 sessions, why not
FX); `concerns-for-later.md` (the remaining 2×).

## Record book

The "biggest since" lookup is saved between runs in `data/tremor/record_book.parquet`
(gitignored, kept in the Actions cache), as of a checkpoint 14 days back. A warm run reads its
warm-up plus the bars after the checkpoint and publishes events after it: 40% of bars instead
of 72%, 48 s instead of 74 s. With no book, an invalid one or a new series, the run goes cold.

**Belongs in:** `architecture.md` (derived data, warm runs); `operations.md` (cache);
`CLAUDE.md` "Working locally".

## Numbers

| | at the freeze | now |
|---|---|---|
| pushes a year | 58.1 | 56.5 |
| reached | 94.4% | 94.4% |
| held at the next close | 74.3% | 74.2% |
| false alarms (as printed) | 85 | 238 |

Most of the 238 are overnight events, which `tools/report_card.py` judges by the first hour's
own move. Left out, the count is about 96. **Open:** judge them by the gap.

## Drafts

**`architecture.md`, "1. The return":**

> **1. The return, and the gap before it.** Each hourly bar is scored on the move inside it. But
> markets close: a fund from 16:00 to 09:30, a currency pair over the weekend. What happens
> meanwhile shows up as a jump from the last price before the close to the first after the
> open. That is the **gap**, a separate reading of its own, compared with the instrument's own
> usual gap after the same kind of close (a Monday with other Mondays). It goes through the
> same checks and rarity rungs as an hour and makes its own events. There is still one event
> per instrument per day: when the gap and an hour both fire, the rarer one describes the day.
> A gap is left unscored when it might not be the market (an unconfirmed dividend, a split, a
> missing bar before the close). Crypto never closes, so it has no gap.

**`architecture.md`, derived data:**

> Only the bars are committed. Everything computed from them (metrics, residuals, the events
> table, the record book) is gitignored. The hourly job keeps the metrics and the record book in
> the Actions cache and extends them, and rebuilds from the bars only when that cache is missing
> or `config_version` has moved.

**`README.md` quick start, point 3:**

> 3. **Settings → Secrets and variables → Actions → Secrets.** Minimum required:
>    `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`. `TELEGRAM_HEALTH_CHAT_ID` is optional and
>    diverts health and provider failures out of the product chat. Every other secret is a data
>    provider, and which ones you need is decided by `config/basket.yaml`: each instrument names
>    its `provider`. A missing key costs you those instruments and nothing else. **To add a
>    provider:** a client module in `price_monitor/` exposing `fetch_full_history(...)`, its name
>    in `PROVIDERS` in `tremor/basket.py`, a branch in `tremor/backfill.py`, and the secret in
>    the workflows' `env:`. `source` is identity and never follows a provider change.

## Found while reading

- `architecture.md` says "Six commands"; `CLAUDE.md` lists four.
- "About two minutes" for a rebuild (four docs): now 2.5–3 minutes locally.
- `CLAUDE.md`: "~820 tests, about four minutes" is now 885 tests, about six minutes; its table of
  documents doesn't list this file.

## To watch

- **2026-10-01:** the monthly payers go ex-dividend. Check that Yahoo lists them by the 10:05 run.
- The first hourly run after a formula change is a full rebuild.
