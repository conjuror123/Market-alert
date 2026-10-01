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

## 1. Thirteen instruments are walled at 2020-02-10

Verified on the store: thirteen instruments hold no bar before 2020-02-10, and XLP holds
none before 2022-08-01. Everything else reaches 2002–2007, and the currency pairs reach
2003.

The wall is a provider plan limit, not a bug. The consequence is that those instruments
are **quieter than the rest by design**: an instrument with six years of history cannot
say "the biggest since 2008".

This entry used to add that five instruments could not reach the top rung at all, and
blamed the wall for it. **Four of those five were the ladder, not the history.** Under the
re-cut table only SOL-USD never reaches `extreme`; LINK-USD, XLP and XLU now do, and
EUR/USD — which was on the list with twenty-three years and 145,000 bars — was never a
history problem at all. What is left is the true version of the claim: a short history
caps the DATE a message can quote, not the rung it can reach.

XLP at 2022-08-01 is separate and unexplained — 7,246 bars against XLK's 11,572, from the
same provider on the same plan. Nothing in the repository accounts for the difference.

**What acting on it would mean:** deepening history from an archive provider, which is a
data migration rather than a code change, plus finding out what is different about XLP.

---

## 2. Repository size

565 MiB packed, 590 MiB on disk. GitHub starts warning at 1 GB.

The committed bars are only 84 MiB of that; the rest is history — every rewrite of every
parquet since the store began, which git cannot delta because parquet is compressed.
Sharding the live year by month (already done) slowed the growth; it did not undo what is
already in the history.

**What acting on it would mean:** rewriting history to drop superseded parquet blobs,
which force-pushes the branch production runs from and is irreversible. That is the
reason it has not been done, not the effort.

---

## 3. The feed comparison rests on 62 hours

**What the feed split is.** The basket is not served by one data provider. Each
instrument is assigned to the feed that was shown to price *it* correctly:

| provider | instruments | what it is |
|---|---|---|
| `tiingo` | 29 | IEX — a single exchange |
| `sifting` | 8 | FX; aggregated across venues, 0.11–0.35 bps median against the stored bars |
| `yahoo` | 15 | a consolidated feed — an undocumented endpoint with no SLA, which can change shape without notice. That is why the funds are split across two providers rather than sent to one |
| `coinbase` | 9 | the exchange itself, for crypto |

**Why the assignment needed measuring at all.** IEX is one exchange holding roughly 5.5%
of US equity volume; a consolidated feed sees the whole tape. Two feeds can both be
complete and still disagree about where a thin ETF closed at 14:00, because they saw
different prints. On a liquid fund that difference is a fraction of a basis point. On a
thin single-commodity fund it is not — and a few basis points of disagreement, against an
hourly sigma of 20–40 bps, is not noise. **It is an alert for a move that never
happened**, arriving through the data rather than through the arithmetic. That is the one
failure worth paying to avoid, and it is why fifteen thin funds stayed on the
consolidated feed instead of moving with the rest.

**What was actually measured.** `tools/tiingo_compare.py` fetched the same hours from
Tiingo, folded them to the hourly grid with the same code the pipeline uses, joined them
to the bars already stored, and reported the median absolute difference in basis points,
per instrument. Instruments that agreed closely moved; instruments that did not, stayed.

**The concern.** That comparison covered **62 overlapping hours — one week**, and has not
been repeated since. One week is one volatility regime. A feed that tracks the
consolidated tape closely in a quiet week can diverge in a violent one, precisely when an
alert matters most, because that is when prints scatter across venues. Several
instruments sat close to the line where the decision flips; their assignment rests on a
sample too small to separate them confidently.

Nothing is known to be wrong. The point is that the evidence is thin, and the assignment
it produced is treated as settled.

**What acting on it would mean:** re-running `tools/tiingo_compare.py` — 44 requests,
read-only, runs in Actions where both the key and the store are — over a longer and
ideally more volatile window, and recording the sample size as a limit next to the
result. Cheap. It is on this list rather than the other one only because nothing is
visibly broken.

**The 2026-09-22 Alpaca probe is a second, independent measurement of the same thing, and
it confirms the split.** `tools/alpaca_compare.py` put Alpaca's two feeds against the
stored bars over 30 days, all 44 US-listed instruments:

| | agrees with the store | median disagreement | volume share |
|---|---|---|---|
| Alpaca **SIP** (consolidated) | 44 of 44 | **0.00 bps** | 119.6% |
| Alpaca **IEX** (one exchange) | 30 of 44 | 0.86 bps | 7.5% |

The fourteen IEX disagrees with are *exactly* the thin commodity funds — BNO CORN CPER
DBA DBB DBC PALL PPLT SLV SOYB UGA UNG USO WEAT — and the size is not marginal: **UGA
disagrees by 15.09 bps at the median and 60.17 at the ninetieth percentile**, which
against an hourly sigma of 20–40 bps is one and a half to three sigma. That is a
`noticeable` event manufactured by the feed. The decision to keep fifteen thin funds off
a single-exchange feed was right, and is now supported by a second provider rather than
by one 62-hour sample.

It also raises the confidence in the stored bars themselves: an independent consolidated
tape agrees with them to the cent on the median hour.

---

## 4. Better and deeper data

The general version of concerns 1 and 3: more history, better-verified providers, and
more instruments. Grouped here because they are one programme rather than three tasks,
and because each of them is a data migration with a verification step rather than a code
change.

---

## 5. The weekend yardstick is the noisiest in the jump detector

A weekend gap is judged against the 26 weekends of the half-year before it, so its yardstick
is uncertain by about ±16% (a fund's hours: ±3%, its nights: ±8%). Measured, pooling
weekends with nights would predict the next weekend a little better (1.94 against 2.10), and
a 2-year window better still for currency pairs (2.96 against 3.90). Separate kinds on one
half-year were kept for one rule across both calendars (`docs/decisions.md`).

**What acting on it would mean:** pooling a fund's weekends with its nights, or a longer
window for weekends only — each a one-line change in `tremor/jumps.py` and a re-run of
`tools/stage_report.py`.

---

## 6. A wider basket: every source measured, and what is still unsourced

The plan below adds 96 funds, 9 currency pairs and 7 coins. Each source was measured for
it on 2026-09-30 (`tools/fund_verdict.py`, `tools/sifting_probe.py`), and each was asked
only what it alone can answer.

| source | live hourly? | history | its job here |
|---|---|---|---|
| Tiingo IEX | yes — 50/hour, shared with production | — | liquid funds, IEX-safe only |
| Alpaca IEX | documented real-time on the free plan; not yet measured at :05 | from about 2021 | IEX-safe funds, with no quota, *if* fresh |
| Alpaca SIP | no — refuses queries ending under 15 minutes ago | from 2016, many symbols a call | the reference tape; seeding every new fund |
| Yahoo | yes — no published limit, no SLA | 60 days at 30 minutes | funds that need the consolidated tape |
| Twelve Data | yes — 800/day, but 8/minute | EM pairs from 2019–2020 | archive; live, a minute of every run per 8 funds |
| SiftingIO | yes — 10,000/month | FX from 2000–2012, except KRW INR BRL | every FX pair; 5 funds |
| Coinbase | yes — no key | from each coin's listing | crypto |
| Finnhub | quotes only on the free tier | — | nothing: a quote at :05 is not the :00 close |
| FRED | daily series | — | VIX |
| HF Data | — | US funds before 2020 | deepening only |

Alpaca SIP's 15-minute wall was confirmed on 2026-09-22 (403 at 2, 5 and 10 minutes,
served at 15). Moving the run to :20 or paying for the unrestricted tier would lift it, and
both were declined.

**The fund verdict.** The reference is Alpaca's consolidated tape over 28 days of
regular-session hours. A feed passes when its hourly closes agree to median ≤ 2 bps and
p90 ≤ 5, **and** it lacks at most 2% of the tape's hours: a missing hour is a move never
scored.

- **7 of the 96 no longer trade:** `JJC JJN JJU JO NIB BAL COW`. They are not on the tape
  at all.
- **The other 89 all reach back past 2020-02-10** on Alpaca's history.
- **IEX agrees on 30:** `KRE XRT IHI SMH SOXX IGV DIA RSP EWJ FXI EWZ INDA EWT EPI VNQ IYR
  GOVT VGIT SPTL VTIP SCHP VMBS LMBS VCIT VCSH IGIB SPIB USIG USHY EMLC`. IEX here means
  Tiingo's feed or Alpaca's, which are the same exchange.
- **Yahoo agrees on 87.** It misses TUR (p90 5.6) and RWX (3% of hours missing) by a
  hair. On TUR, Twelve Data misses the tape by exactly the same amounts, spread over every
  hour of the day, so that is how thin TUR trades rather than a bad feed.
  - **Since 2026-10-01 both are in.** RWX on Yahoo: a missing hour is now a hole, skipped
    rather than merged into the next, so 3% missing costs readings, not truth. TUR on
    Google Finance's quote page, whose hourly closes matched the tape exactly on the one
    session measured (`tools/google_probe.py`). Google sees exchange trades only — 91% of
    TUR's volume, 19% of RWX's — which is why RWX is not on it.
- **Twelve Data equals Yahoo** on all eight names checked. It is the consolidated tape too.
- **SiftingIO carries few of the candidates at all.** Five pass: `DIA MDY IAU SGOL SIVR`.
- **The 44 held funds reproduce today's split, with two exceptions.** USO and SLV sit on
  Tiingo and fail IEX, at 3.3/15.2 and 1.6/5.4 bps, as they did in the 2026-09-22
  comparison.

**What fits where:**

- The 30 IEX-safe funds exceed Tiingo's 21 free slots, or 23 if USO and SLV leave it.
- The other 59 need a consolidated feed:
  - Yahoo;
  - Twelve Data, costing a minute of every run per eight funds;
  - SiftingIO's spare, about 1,100 calls a month with 17 pairs, which is roughly the 5
    funds it passes.

**FX.** SiftingIO serves all nine new pairs live. For USD/KRW, USD/INR and USD/BRL its
hourly history starts on 2026-07-26. Twelve Data reaches further back for all three:

| pair | Twelve Data from | agrees with SiftingIO? |
|---|---|---|
| USD/KRW | 2020-01 | yes, 0.38 bps median |
| USD/INR | 2019-11 | yes, 0.38 bps median |
| USD/BRL | 2019-09 | **no**, 1.54 median / 6.94 p90 even inside 11:00–22:00 UTC |

The other six new pairs go back to 2003–2007 on SiftingIO itself.

**Crypto.** Coinbase lists all seven:

| coin | listed from |
|---|---|
| XRP | 2019 |
| ATOM | 2020-01 |
| UNI | 2020-09 |
| FIL | 2020-12 |
| AAVE | 2020-12 |
| DOT | 2021-06 |
| POL | 2024-09, as the renamed MATIC (MATIC-USD: 2021-03 to 2024-09) |

POL's two histories would have to be joined.

**Still without a source:**

1. **The seven delisted ETNs:** nickel, aluminium, tin, coffee, cocoa, cotton and
   livestock. Without them, the industrial-metals and agriculture blocks stop at five
   members each.
2. **A second consolidated live feed for about 60 funds.** The alternative is to accept
   Yahoo carrying them, which is the concentration this document already worries about.
   Twelve Data can take a slice, at a minute of run time per eight.
3. **USD/BRL's history before 2026-07-26 from a feed that agrees with SiftingIO.**
   Otherwise, seed it from Twelve Data at the disagreement above, or let it start young.
4. **Live IEX capacity past Tiingo's 21 slots, if Alpaca's IEX turns out not to be
   fresh at :05.**

**The four questions any future candidate has to answer**, cheapest disqualifier first:

1. **Freshness.** Does it serve the bar that closed at :00 by :05?
2. **Headroom.** How many requests per hour does it allow, against the instruments it
   would carry?
3. **Agreement** against the consolidated tape, on the thin names.
   `tools/fund_verdict.py` measures it.
4. **Coverage and depth.** Which tickers exist, and do they reach 2020-02-10?

---

### The plan it blocks

Parked 2026-09-22 and written for the previous detector; kept here so it outlives the
session it was made in. **The owner's decisions:** one re-cut of the whole basket and block
map at once; message volume may grow, and what to do about it is chosen after seeing it; no
instrument shallower than the existing 2020-02-10 wall.

**The rule it rests on:** every block ends with 8–12 members. Measured on the previous
detector's residuals, leftover correlation between members falls steeply up to about six
and is flat after eight; splitting today's thin blocks without adding members made it
worse. **Under the jump detector the blocks do not enter detection until stages 7 and 8**
(`architecture.md`), so the rule binds then, not now — a new instrument today is judged on
its own history alone.

**The target**, about 150 instruments in 16–19 blocks, existing ones in bold, broad
benchmarks (SPY IWM DIA RSP MDY) watched outside the basket:

| block | members |
|---|---|
| US cyclicals | **XLY XLI XLB XLF XLE** KRE XRT ITB IYT XME |
| US defensives | **XLP XLV XLU** XBI XPH IHI VDC VHT VPU |
| US tech | **XLK XLC QQQ** SMH SOXX IGV FDN CIBR SKYY |
| developed ex-US | **EFA** EWJ EWG EWU EWQ EWC EWA EWL EWN EZU |
| emerging | **EEM** FXI EWZ EWW INDA EWY EWT EZA EPI TUR |
| real estate | **XLRE** VNQ IYR RWR SCHH REM VNQI RWX |
| government bonds | **SHY IEI IEF TLH TLT** GOVT SCHO VGIT VGLT SPTL BWX |
| inflation and securitized | **TIP MBB** VTIP SCHP STIP VMBS SPMB LMBS JMBS |
| IG credit | **LQD** VCIT VCSH IGIB SPIB USIG QLTA GIGB SLQD |
| HY credit | **HYG JNK BKLN PFF** SHYG USHY ANGL SRLN FALN |
| EM credit | **EMB** EMLC VWOB PCY EBND LEMB EMHY CEMB |
| energy | **USO BNO UGA UNG DBC** DBO DBE UNL |
| precious metals | **GLD SLV PPLT PALL** IAU SGOL SIVR GLTR |
| industrial metals | **DBB CPER** JJC JJN JJU LIT REMX SLX |
| agriculture | **DBA CORN WEAT SOYB** CANE JO NIB BAL COW |
| DM FX | **EUR/USD USD/JPY GBP/USD USD/CHF AUD/USD NZD/USD USD/CAD** USD/SEK USD/NOK |
| EM FX | **USD/CNH** USD/MXN USD/ZAR USD/BRL USD/TRY USD/INR USD/KRW USD/PLN |
| crypto majors | **BTC ETH SOL LTC BCH ADA DOGE** XRP |
| crypto alts | **LINK AVAX** DOT MATIC UNI ATOM FIL AAVE |

Expected to fail the feed test first: the MBS funds past MBB/VMBS, EM credit past
EMB/EMLC/VWOB, the JJ* ETNs, and COW/NIB/BAL; a block that cannot reach 8 merges rather
than ships short. **Phase A**, the verdict table, is the measurement at the top of this
item (2026-09-30): the ETNs are gone from the market altogether, and the MBS and EM credit
funds pass on the consolidated tape but not on IEX. Its other half, guards on the previous
detector's severity tables and block labels, went with that detector.

## 8. Smaller things, found and left alone

- **A broken print under 1,000σ still counts.** On 2017-04-15 Coinbase reopened after
  three hours down on a BTC print of $0.06 and an ETH print of $74.98 (the market was at
  $1,183 and $48.5). The 1,000σ line drops BTC's (+1,526σ), but ETH's reads as -36σ, an
  `extreme` hour, and stays. It is history only; nothing in the last year is like it.
- **USD/BRL needs a session of its own** before it is added: it trades 11:00–22:00 UTC on
  97% of days and only sometimes outside it, so without one its nights are holes and the
  São Paulo open is the bar after one.
- **One instrument's timeout turns the whole run red.** A single provider read timeout in
  backfill fails the job and sends the "Failed" email even though every other instrument
  ran.
- **To watch on 2026-10-01:** the monthly payers go ex-dividend; check that Yahoo lists
  them by the 10:05 New York run, or their gaps stay unscored that day.
- **A fund's opening hour fires about twice as often** as its other hours, from extreme
  outliers at the open. The jump detector's time-of-day stage (5) is where this is
  answered.

---

## 7. Comments and prose that have drifted

Small, cosmetic, and worth a pass rather than a project. Nothing specific is currently
listed here — the prose drift that was on this list turned out to be one substantive
error rather than a cosmetic one, and is closed in `docs/decisions.md`.

The standing rule is the useful part: **a comment that describes a mechanism is a claim,
and claims go stale silently.** Prose that merely reads awkwardly can wait. Prose that
asserts how something works cannot, because the next reader will believe it.
