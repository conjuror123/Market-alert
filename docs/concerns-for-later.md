# Concerns for later

> **Holds** what is known, measured where possible, and deliberately not being worked on.
> **Does not hold** settled questions (`decisions.md`) or operational limits that are
> simply how it works (`operations.md`).
> **Add an entry when** something is found and left alone. Say what it is, what it costs,
> and what acting on it would mean — so that leaving it stays a decision rather than an
> oversight.

Nothing here is a bug in the sense of producing a wrong message today.

**Two standing limits that will not change.** It does not predict, and does not claim to:
every number is about a move that already happened, and the word says how unusual it was,
not what comes next. And no record can be longer than an instrument's own history — a
newly added name says "biggest in a quarter" for years before it can say "biggest in six".

---

## 1. Repository size

636 MiB packed (2026-10-02). GitHub starts warning at 1 GB.

The committed bars are only 199 MiB of that; the rest is history — every rewrite of every
parquet since the store began, which git cannot delta because parquet is compressed.
Sharding the live year by month (already done) slowed the growth; it did not undo what is
already in the history.

The bars commit once a day (`operations.md`): about 18 MiB of git a year at today's basket,
simulated.

**What acting on it would mean:** rewriting history to drop superseded parquet blobs,
which force-pushes the branch production runs from and is irreversible. That is the
reason it has not been done, not the effort.

---

## 2. The fund verdict rests on 28 days of one regime

Which feed each fund is on (`decisions.md`, "The data") was measured over 28 days to
2026-09-30, and Sina's agreement over 28 days to 2026-10-02 — one calm stretch. Feeds that
agree to a basis point in a quiet month can part in a violent one, when prints scatter
across venues and an alert matters most. Nothing is known to be wrong.

**What acting on it would mean:** re-running `tools/fund_verdict.py` and `tools/sina_probe.py`
after the next violent month. They can only look back as far as each feed serves (Yahoo 55
days, Sina about 78), so the window has to be caught while it is recent.

---

## 3. The weekend yardstick is the noisiest in the jump detector

A weekend gap is judged against the 26 weekends of the half-year before it, so its yardstick
is uncertain by about ±16% (a fund's hours: ±3%, its nights: ±8%). Measured, pooling
weekends with nights would predict the next weekend a little better (1.94 against 2.10), and
a 2-year window better still for currency pairs (2.96 against 3.90). Separate kinds on one
half-year were kept for one rule across both calendars (`docs/decisions.md`).

**What acting on it would mean:** pooling a fund's weekends with its nights, or a longer
window for weekends only — each a one-line change in `tremor/jumps.py` and a re-run of
`tools/stage_report.py`.

---

## 4. Most of the basket rides undocumented endpoints

Yahoo (34 funds, 4 futures, the dividend check), Sina Finance (33 funds and the LME's
metals) and Google Finance (TUR) are web endpoints with no terms that allow this use and no
notice before they change. A changed shape raises, and the run's health message names the
provider and its instruments; the funds are split between Sina and Yahoo within each block
so either can fail without silencing a block; the LME's metals have no second source.
Tiingo's key is shared with production until the switch.

**What acting on it would mean:** a paid consolidated feed — Alpaca's unrestricted SIP
would carry every fund — and paid futures data (Barchart, Financial Modeling Prep).

---

## 5. History no free source reached

A record shorter than six months scores from the paper's minimum and says "biggest since"
only as far back as it goes. Below each record's start (`architecture.md`, "Where the data
lives"), what is missing and what was tried:

| instruments | missing before | tried |
|---|---|---|
| live cattle | 2024-05 | Yahoo's hourly stops at 730 days; Dukascopy has none; Stooq needs a login |
| coffee, cocoa, cotton | 2019-01 | Dukascopy's CFDs held no traded hour in June 2017; 2018 not yet fetched |
| tin, nickel, aluminium (LME) | 2026-07 | Sina serves the last 1,023 bars; Kitco's nickel (2020-09 on) is a quote that does not track the LME's price; no free hourly LME history found |
| USD/BRL, USD/INR, USD/KRW | 2019-09, 2019-11, 2020-01 | Twelve Data's start; Dukascopy holds no traded hour of INR or KRW in any sampled year, and its BRL files (from 2007) none in 2019; Sina forex holds six months; TradingView about 6,300 bars |

**What acting on it would mean:** paid data for the futures and the three pairs.

---

## 7. Smaller things, found and left alone

- **A broken print under 1,000σ still counts.** On 2017-04-15 Coinbase reopened after
  three hours down on a BTC print of $0.06 and an ETH print of $74.98 (the market was at
  $1,183 and $48.5). The 1,000σ line dropped BTC's (+1,526σ), but ETH's read as -36σ, an
  `extreme` hour. Both were Coinbase's; the coins are Binance's since 2026-10-02, and
  the largest reading in Binance's record is 56σ (UNI, 2024-02-23, the fee-switch vote).
- **One instrument's timeout turns the whole run red.** A single provider read timeout in
  backfill fails the job and sends the "Failed" email even though every other instrument
  ran.
- **To watch at the first month start after the switch:** the monthly payers go
  ex-dividend; check that Yahoo lists them by the 10:05 New York run, or their gaps stay
  unscored that day. (Due 2026-10-01, but this branch was not running then.)
- **A fund's opening hour fires about twice as often** as its other hours, from extreme
  outliers at the open. The jump detector's time-of-day stage (5) is where this is
  answered.

---

---

## 8. Comments and prose that have drifted

Small, cosmetic, and worth a pass rather than a project. Nothing specific is currently
listed here — the prose drift that was on this list turned out to be one substantive
error rather than a cosmetic one, and is closed in `docs/decisions.md`.

The standing rule is the useful part: **a comment that describes a mechanism is a claim,
and claims go stale silently.** Prose that merely reads awkwardly can wait. Prose that
asserts how something works cannot, because the next reader will believe it.
