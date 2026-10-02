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

## 1. TUR keeps one weekday a week under the weekly bar commit

TUR's feed, Google Finance's quote page, holds the latest session and nothing earlier. The
bars are committed on Saturdays (`operations.md`) and every run fetches from the newest
committed bar, so from Tuesday on the week's earlier sessions cannot be fetched again, and
Saturday's commit records Friday's alone. Monday to Thursday are lost every week, and the
events found on them disappear when the next day's run rescores without them. Nothing is
affected today — this branch is not on the hourly trigger — but it would be from the
switch.

**What acting on it would mean:** committing TUR's month shard (a few kB) on the first run
after each NYSE close; or moving TUR to Yahoo or Twelve Data, which reach weeks back and miss
the tape on it by p90 5.6 bp.

---

## 2. Repository size

636 MiB packed (2026-10-02). GitHub starts warning at 1 GB.

The committed bars are only 199 MiB of that; the rest is history — every rewrite of every
parquet since the store began, which git cannot delta because parquet is compressed.
Sharding the live year by month (already done) slowed the growth; it did not undo what is
already in the history.

**What acting on it would mean:** rewriting history to drop superseded parquet blobs,
which force-pushes the branch production runs from and is irreversible. That is the
reason it has not been done, not the effort.

---

## 3. The fund verdict rests on 28 days of one regime

Which feed each fund is on (`decisions.md`, "The data") was measured over 28 days to
2026-09-30, and Sina's agreement over 28 days to 2026-10-02 — one calm stretch. Feeds that
agree to a basis point in a quiet month can part in a violent one, when prints scatter
across venues and an alert matters most. Nothing is known to be wrong.

**What acting on it would mean:** re-running `tools/fund_verdict.py` and `tools/sina_probe.py`
after the next violent month. They can only look back as far as each feed serves (Yahoo 55
days, Sina about 78), so the window has to be caught while it is recent.

---

## 4. The weekend yardstick is the noisiest in the jump detector

A weekend gap is judged against the 26 weekends of the half-year before it, so its yardstick
is uncertain by about ±16% (a fund's hours: ±3%, its nights: ±8%). Measured, pooling
weekends with nights would predict the next weekend a little better (1.94 against 2.10), and
a 2-year window better still for currency pairs (2.96 against 3.90). Separate kinds on one
half-year were kept for one rule across both calendars (`docs/decisions.md`).

**What acting on it would mean:** pooling a fund's weekends with its nights, or a longer
window for weekends only — each a one-line change in `tremor/jumps.py` and a re-run of
`tools/stage_report.py`.

---

## 5. Most of the basket rides undocumented endpoints

Yahoo (34 funds, 5 futures, the dividend check), Sina Finance (33 funds and tin), Google
Finance (TUR) and Kitco (nickel) are web endpoints with no terms that allow this use and no
notice before they change. A changed shape raises, and the run's health message names the
provider and its instruments; the funds are split between Sina and Yahoo within each block
so either can fail without silencing a block. Kitco's glitch rule holds a real 15%
five-minute move back a day. Tiingo's key is shared with production until the switch.

**What acting on it would mean:** a paid consolidated feed — Alpaca's unrestricted SIP
would carry every fund — and paid futures data (Barchart, Financial Modeling Prep).

---

## 6. History no free source reached

A record shorter than six months scores from the paper's minimum and says "biggest since"
only as far back as it goes. Below each record's start (`architecture.md`, "Where the data
lives"), what is missing and what was tried:

| instruments | missing before | tried |
|---|---|---|
| live cattle, aluminium | 2024-05 | Yahoo's hourly stops at 730 days; Dukascopy has neither; Stooq needs a login |
| coffee, cocoa | 2024-05 | as above; Dukascopy's CFDs being measured |
| cotton | 2026-06 | Yahoo's continuous series too broken to use; Dukascopy's CFD being measured |
| tin (Shanghai) | 2019-08 | Sina serves no older contract |
| nickel | 2020-09 | Kitco's first quote is 2020-09-11 |
| USD/BRL, USD/INR, USD/KRW | 2019-09, 2019-11, 2020-01 | Twelve Data's start; Dukascopy has no INR, and its BRL files (from 2007) hold no traded hour; KRW being measured; Sina forex holds six months; TradingView about 6,300 bars |
| ATOM UNI FIL AAVE POL ADA DOGE DOT SOL AVAX | their Coinbase listing, 2020–2021 | Bitstamp lists none earlier. Binance's archive does, in USDT: ADA from 2018-04, POL (as MATIC) and ATOM 2019-04, DOGE 2019-07, DOT and SOL 2020-08, AVAX 2020-09; UNI, FIL, AAVE no earlier than Coinbase |

**What acting on it would mean:** paid data for the futures and the three pairs; for the
coins, the same gated fill XRP had (`tools/bitstamp_fill.py`), from Binance with each hour's
USDT turned into dollars (Coinbase's BTC-USD over Binance's BTC/USDT would do it, and would
take out USDT's own swings, such as its premium in March 2020).

---

## 7. The blocks are still the nine broad ones

The basket grew from 61 to 173 without its blocks being re-cut: equity holds 61, credit 26,
rates 20. The jump detector reads each instrument alone, so this changes no message today;
it matters from stages 7 and 8 (block co-jumps, own move), which test against the blocks.

**The re-cut planned for then (2026-09-22):** every block ends with 8–12 members. Measured
on the previous detector's residuals, leftover correlation between members falls steeply up
to about six and is flat after eight; splitting the thin blocks without adding members made
it worse. The target, 16–19 blocks, with the delisted ETNs' places taken by the
commodities themselves:

| block | members |
|---|---|
| US cyclicals | XLY XLI XLB XLF XLE KRE XRT ITB IYT XME |
| US defensives | XLP XLV XLU XBI XPH IHI VDC VHT VPU |
| US tech | XLK XLC QQQ SMH SOXX IGV FDN CIBR SKYY |
| developed ex-US | EFA EWJ EWG EWU EWQ EWC EWA EWL EWN EZU |
| emerging | EEM FXI EWZ EWW INDA EWY EWT EZA EPI TUR |
| real estate | XLRE VNQ IYR RWR SCHH REM VNQI RWX |
| government bonds | SHY IEI IEF TLH TLT GOVT SCHO VGIT VGLT SPTL BWX |
| inflation and securitized | TIP MBB VTIP SCHP STIP VMBS SPMB LMBS JMBS |
| IG credit | LQD VCIT VCSH IGIB SPIB USIG QLTA GIGB SLQD |
| HY credit | HYG JNK BKLN PFF SHYG USHY ANGL SRLN FALN |
| EM credit | EMB EMLC VWOB PCY EBND LEMB EMHY CEMB |
| energy | USO BNO UGA UNG DBC DBO DBE UNL |
| precious metals | GLD SLV PPLT PALL IAU SGOL SIVR GLTR |
| industrial metals | DBB CPER LIT REMX SLX nickel aluminium tin |
| agriculture | DBA CORN WEAT SOYB CANE coffee cocoa cotton live cattle |
| DM FX | EUR/USD USD/JPY GBP/USD USD/CHF AUD/USD NZD/USD USD/CAD USD/SEK USD/NOK |
| EM FX | USD/CNH USD/MXN USD/ZAR USD/BRL USD/TRY USD/INR USD/KRW USD/PLN |
| crypto majors | BTC ETH SOL LTC BCH ADA DOGE XRP |
| crypto alts | LINK AVAX DOT POL UNI ATOM FIL AAVE |


---

## 8. Smaller things, found and left alone

- **A broken print under 1,000σ still counts.** On 2017-04-15 Coinbase reopened after
  three hours down on a BTC print of $0.06 and an ETH print of $74.98 (the market was at
  $1,183 and $48.5). The 1,000σ line drops BTC's (+1,526σ), but ETH's reads as -36σ, an
  `extreme` hour, and stays. It is history only; nothing in the last year is like it.
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

## 9. Comments and prose that have drifted

Small, cosmetic, and worth a pass rather than a project. Nothing specific is currently
listed here — the prose drift that was on this list turned out to be one substantive
error rather than a cosmetic one, and is closed in `docs/decisions.md`.

The standing rule is the useful part: **a comment that describes a mechanism is a claim,
and claims go stale silently.** Prose that merely reads awkwardly can wait. Prose that
asserts how something works cannot, because the next reader will believe it.
