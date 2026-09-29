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
| `tiingo` | 37 | IEX — a single exchange |
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

## 6. A wider basket still has no live feed, and that is the whole blocker

The plan to widen the basket needs a provider for roughly 63 new US-listed instruments
plus 7 EM currency pairs. Measured on 2026-09-22, here is what is and is not covered.

**History is not the problem.** Twelve Data already reaches past the 2020-02-10 floor
these instruments would be held to, and seeding them is about 630 requests against an
800-a-day budget — roughly 90 minutes of wall clock, not the multi-day job it was once
described as. Alpaca's free SIP reaches 2016 and would be faster, but it duplicates
something that already works.

**The live hourly fetch is the problem, and nothing found so far moves it.** Tiingo's
50-an-hour bucket has about 13 free slots against 37 used. Yahoo publishes no limit but
is an undocumented endpoint with no SLA, so putting sixty instruments behind it
concentrates most of the basket on the one feed this document already worries about.

**Alpaca cannot help here, and the reason is measured rather than assumed.** On the free
plan its SIP feed refuses any query ending less than 15 minutes ago — 403, *"subscription
does not permit querying recent SIP data"*, confirmed at 2, 5 and 10 minutes and served
at 15, 20 and 30. The hourly run fires at :05 and needs the bar that closed at :00. Two
remedies exist and both were declined: move the run to :20, or pay for the unrestricted
tier.

Also established about Alpaca, so the next reader does not re-derive it:

- Its **IEX** feed only reaches about 2021, so it is shallow as well as wrong on thin
  names.
- It serves **63 of the 70** candidate instruments. The seven it does not are `JO NIB BAL
  COW JJC JJN JJU` — coffee, cocoa, cotton, livestock and three base-metal ETNs.
- Its **forex endpoint is not on the free plan at all**: 403, *"forbidden: insufficient
  grants"*. EM FX gets nothing from it.

**The four questions any future candidate has to answer**, in this order, because the
cheapest disqualifier comes first:

1. **Freshness.** Will it serve a bar five minutes old? If not, it cannot be the live
   feed, whatever else it does well.
2. **Headroom.** Requests per hour against 63 more instruments.
3. **Agreement.** Median and p90 disagreement in bps against the stored bars, on the THIN
   names — the liquid ones agree with everything. `tools/alpaca_compare.py` is the shape
   to copy; the line is median ≤ 2 bps and p90 ≤ 5.
4. **Coverage and depth.** Which tickers exist, and do they reach 2020-02-10.

**What acting on it would mean:** finding a provider that answers question 1. Until one
does, the widening is capped at the ~13 Tiingo slots, or accepts Yahoo concentration as a
deliberate risk. `tools/alpaca_probe.py` and `tools/alpaca_compare.py` are written and
generalise to the next candidate with a change of endpoint.

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
than ships short. **Phase A** — a committed verdict table, one row per candidate: provider,
median and p90 disagreement in bps against a trusted feed, oldest bar, pass or reject — was
never produced; it stopped at the Alpaca measurements above. Its other half, guards on the
previous detector's severity tables and block labels, went with that detector.

## 8. Smaller things, found and left alone

- **A hole inside a session makes a two-hour move.** When a bar is missing mid-session, the
  next bar's return spans both hours and is judged as one. Rare; acting on it means
  dividing by the elapsed time or leaving that bar unscored.
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
