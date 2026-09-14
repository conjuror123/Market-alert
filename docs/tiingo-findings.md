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
basis points, over 27 overlapping hours (124 for FX).

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
treated as settled; `tools/tiingo_compare.py` does it in 44 requests.

## Where the request budget actually goes

Simulated over twelve weeks against the NYSE calendar, mirroring
`backfill.nothing_can_have_appeared`:

```
44 ETFs   277/day    6.29 each - skipped while the market is shut
 8 FX     192/day   24    each - never skipped, no session table to skip by
TOTAL     469/day  = 59% of Twelve Data's 800
```

The eight currency pairs are **41% of every request the monitor makes** while
being 15% of the basket. Routine hourly runs are not what exhausts the daily
quota - they sit at 59%. Backfills are.

Separately: 47 of the 168 hours in a week carry no FX bar at all (all of
Saturday, Sunday up to 21:00 UTC, Friday 23:00). That is 54 requests a day,
11% of total demand, asked and answered empty.

## The dividend bug in `docs/decisions.md`, confirmed and solvable

`decisions.md` records that Twelve Data's `adjust=all` series divides the
nominal pre-split dividend by the split-adjusted price, so every dividend step
before 2025-12-05 on the five SPDRs that split 2:1 reads twice its true size -
which fails `verify_alignment`'s 25 bps tolerance and leaves those five with six
years of history instead of twenty-four. The recorded fix is to buy a third
Twelve Data series at `adjust=none` and take a ratio.

Tiingo publishes `divCash` and `splitFactor` as declared values on the free
daily endpoint. Measured against them, the stored step is **exactly 2.00x on all
fifteen ex-dates tested**, across all five tickers:

```
ticker  ex-date      divCash  prev_close   true_step  stored_step  ratio
XLK     2024-06-24    0.4000      228.41    0.001751     0.003502  2.00x
XLE     2024-09-23    0.7275       88.76    0.008196     0.016392  2.00x
XLU     2024-12-23    0.6280       76.43    0.008217     0.016433  2.00x
...
```

So the bug is confirmed from an independent source, and the inference step that
produces it can be replaced by a declared amount rather than repaired.
`splitFactor` also states the 2:1 split directly, which `SPLIT_THRESHOLD` has
never been able to see because both Twelve Data series are split-adjusted and
the ratio cancels it.
