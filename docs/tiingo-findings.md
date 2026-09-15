# What a free Tiingo key actually serves

Measured against a real key on 2026-09-14 by `tools/tiingo_probe.py`,
`tools/tiingo_probe2.py`, `tools/tiingo_compare.py` and
`tools/tiingo_fx_and_actions.py`, all run from `.github/workflows/tiingo-probe.yml`
because the key is a repository secret. Nothing below is from documentation.

## The free tier, as it behaves

| | |
|---|---|
| FX | **Included.** `/tiingo/fx/<pair>/prices` serves OHLC at `30min` and `1hour`, back to 2021 at least. The pricing page does not list it. |
| ETF coverage | **44 of 44.** Every ticker in the basket is known to the IEX feed. |
| Volume | Present, but only when asked for: `columns=open,high,low,close,volume`. The default response omits it entirely. |
| Intraday depth | Back to **January 2018** at least. A request returns at most 10000 rows, which is a page cap and not the edge of history. |
| Rate limits | 50 requests/hour, 1000/day, 500 unique symbols/month. The hourly bucket is the binding one. |
| Bandwidth | A bounded 4-day 30-minute request is ~3.6 KB. All 52 symbols at 24 runs a day is **0.135 GB/month** against a 1 GB cap. |
| Pace | **0.281 s/request, no throttling needed.** 52 symbols take about 15 s in sequence, against 416 s at Twelve Data's 8-second spacing. |

The default intraday response covers **today only** (13 bars, 1.2 KB), not the
most recent 2000 bars. Bounding the window with `startDate` makes a request
larger, not smaller; it is still trivial against the cap.

## Does it price the basket the same way?

IEX is one exchange and sees **5.5% of consolidated volume on average** here, so
coverage proves nothing on its own. Every symbol was fetched, folded onto the
hourly grid with `bars.to_hourly`, and joined to the stored Twelve Data bars.
Figures are the absolute difference between the two feeds' hourly close, in
basis points, over 27 overlapping hours (124 for FX). That window is a **limit**,
not a settled sample: borderline names (UGA, SOYB, UNG, WEAT, PPLT, CORN, BKLN,
PALL, DBB, CPER) are not closed. Re-run via `.github/workflows/tiingo-probe.yml`
with `compare: true`, or `python -m tools.tiingo_compare` (44 requests).

An hourly move of one sigma is roughly 20-40 bps for these instruments. So one
bps is about 0.03 sigma and invisible; ten bps is 0.3 sigma and starts to move
the ladder on its own.

**Agree closely** (median under 1 bps): SPY QQQ IWM XLK XLF XLP XLV XLI XLU
XLY XLE XLB XLC EFA EEM SHY IEI IEF TIP MBB LQD JNK EMB PFF TLT TLH HYG GLD
SLV USO BNO, and all eight currency pairs.

**Disagree enough to matter** (median above 2 bps, or a single hour past 10):

| ticker | median | p90 | worst hour |
|---|---|---|---|
| UGA | 7.97 | 22.62 | **76.79** |
| SOYB | 7.22 | 15.20 | 21.88 |
| UNG | 4.75 | 4.96 | 9.90 |
| WEAT | 3.85 | 13.26 | 15.24 |
| PPLT | 3.00 | 4.24 | 6.20 |
| CORN | 2.50 | 5.00 | 7.52 |
| BKLN | 2.43 | 2.43 | 2.43 |
| PALL | 2.11 | 4.23 | 14.25 |
| DBB | 1.90 | 6.70 | 11.41 |
| CPER | 1.25 | 3.75 | 15.36 |

UGA's worst hour is about two sigma of pure feed disagreement - an alert for a
move that did not happen, arriving through the data rather than through the
maths. These are single-commodity and niche-credit funds that trade thinly
enough that IEX's slice of the tape is a different price.

The sample is one week. It should be re-run before any of these lines is
treated as settled; `tools/tiingo_compare.py` does it in 44 requests, and
`tiingo-probe.yml` `compare: true` is the same measurement from Actions.

## Where the request budget actually goes

Simulated over twelve weeks against the NYSE calendar, mirroring
`backfill.nothing_can_have_appeared`:

```
44 ETFs   277/day    6.29 each - skipped while the market is shut
 8 FX     ~138/day  ~17   each - skipped when the Sun 17:00 → Fri 17:00
                                 New York week is shut (54 empty hours/day
                                 used to be asked anyway)
TOTAL     ~415/day = 52% of Twelve Data's 800, and the hourly path no
                       longer spends that budget at all
```

The eight currency pairs were **41% of every Twelve Data request** while being
15% of the basket, because they were never skipped. They now use the same
reference week as the bar walk. Routine hourly runs are not what exhausts a
daily quota - they sit on Tiingo's 50/hour bucket (37 live names in a US-session
hour) and Yahoo (no published cap). Backfills and probes are.

Separately: 47 of the 168 hours in a week carry no FX bar at all (all of
Saturday, Sunday up to 21:00 UTC, Friday 23:00). That used to be 54 requests a
day asked and answered empty. `nothing_can_have_appeared` now skips them.

## The dividend bug in `docs/decisions.md`, confirmed and **fixed**

`decisions.md` recorded that Twelve Data's `adjust=all` series divides the
nominal pre-split dividend by the split-adjusted price, so every dividend step
before 2025-12-05 on the five SPDRs that split 2:1 reads twice its true size -
which fails `verify_alignment`'s 25 bps tolerance and leaves those five with six
years of history instead of twenty-four.

Tiingo publishes `divCash` and `splitFactor` as declared values on the free
daily endpoint. Measured against them, the stored inferred step was **exactly
2.00x on all fifteen ex-dates tested**, across all five tickers:

```
ticker  ex-date      divCash  prev_close   true_step  stored_step  ratio
XLK     2024-06-24    0.4000      228.41    0.001751     0.003502  2.00x
XLE     2024-09-23    0.7275       88.76    0.008196     0.016392  2.00x
XLU     2024-12-23    0.6280       76.43    0.008217     0.016433  2.00x
...
```

So the bug was confirmed from an independent source. Inference is gone:
`tremor.corporate_actions` now derives the step as `d/(1-d)` on the raw previous
close (`divCash / close_{t-1}`), never `adjClose`. Splits are written into
`data/tremor/corporate_actions.csv` for provenance and filtered out of
`load_steps`, so they cannot enter `unadjust_factor`. Twelve Data remains a
labelled `--source twelvedata` fallback that still cannot see splits. Deepening
those five to ~2002 is a separate migration.

## Yahoo, which turned out to matter more

Tiingo was the question; Yahoo was the answer to a different one. The run time
is set by whichever provider is slowest, and after the pairs move to Tiingo the
slowest thing left is the ETFs Tiingo cannot price - sitting on Twelve Data's
8-second pace. So the source worth finding was never another FX feed: it was a
consolidated-tape feed for thin ETFs.

`query1.finance.yahoo.com/v8/finance/chart` needs no key, no account and has no
published quota, answers in 0.77 s, and measured the same way over 62 hours:

| | median | p90 | max | volume vs stored |
|---|---|---|---|---|
| 8 liquid ETFs | 0.00 | 0.00 | 0.00 | ~100% |
| 15 thin ETFs | 0.00 | 0.00 | 0.00-23 | 90-100% |

Zero, not "close" - including every fund where Tiingo's IEX feed drifts. It is
the consolidated tape, and its volume is the stored volume rather than the 2-8%
an IEX-only feed reports.

Splits are back-adjusted the same way the store is: XLK's hourly closes across
the 2:1 of 2025-12-05 run 145.53 then 146.58, which are the two numbers already
on disk. A raw series would have put a 2x step inside a return.

On FX, though, Yahoo is **worse** than Tiingo - 0.43 to 3.26 bps against 0.43 to
1.57 - because its currency quotes are indicative rather than a traded feed.
That is why the pairs did not follow the ETFs across.

### The closing stub

Yahoo appends one extra bar per session at 20:00 UTC carrying no volume and the
same price four times over. It is a marker for the closing instant - the 19:30
bar already holds the closing auction - and Twelve Data does not produce it.
Measured on 14 of 15 tickers, once per session, always at 20:00. Kept, it would
have opened an hourly bucket the session calendar does not expect on every ETF
on every run: a zero-return hour appended after every close. The client drops
it, on zero volume AND zero width together, because an illiquid fund can
legitimately print a whole half-hour at one price.

The first comparison did not catch this: it inner-joined on hours both feeds
had, so a bar only one feed produced was invisible by construction.

## The two that are not live sources, confirmed

Both were tested rather than assumed:

- **FXCM** - archive frozen, nothing past about week 17 of 2026. The client and
  `--deepen-fx` have been deleted. Dukascopy remains.
- **Dukascopy** - publishes whole months only. On 2026-09-14 the August file was
  complete at 744 bars and September returned 404. Fine for history, unusable
  for an hourly top-up.

## Ops visibility, and what this file does not settle

Provider failures and health down/recovered go to `TELEGRAM_HEALTH_CHAT_ID`,
falling back to `TELEGRAM_CHAT_ID` until that secret exists. Product pushes stay
on the product chat. A single dead instrument fails the run and the ops message
names it.

The 27-hour (124 FX) comparison above is a ceiling on what has been measured,
not a verdict on the borderline names. Re-measure with
`.github/workflows/tiingo-probe.yml` (`compare: true`) or
`python -m tools.tiingo_compare` (44 requests) before treating UGA/SOYB/UNG and
the rest of that table as closed.
