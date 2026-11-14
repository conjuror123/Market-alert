# Jump data coverage

Generated 2026-11-13 22:09 UTC by `python -m jump.audit`. One row per instrument: its provider, how far back its bars reach, its hourly volume, and anything wrong with the series (Notes; `—` is sound). Periods end where the bars on disk end: without the open months (`tools/hot_bars.sh restore`), at the last settled month.

## Basket

| Instrument | Block | Provider | Interval | Bars | Period | Days | Bars per day | Price step | Source precision | Volume=0 | Max gap, h | Notes |
|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---|
| `AUD/USD` | FX | sifting | 1h | 146,012 | 2003-08-03 .. 2026-11-13 | 7,291 | 24 | 1e-05 | 1e-06 | 99% | 99 | — |
| `EUR/USD` | FX | sifting | 1h | 148,296 | 2003-05-04 .. 2026-11-13 | 7,374 | 24 | 1e-05 | 1e-06 | 99% | 129 | OHLC: 12 |
| `GBP/USD` | FX | sifting | 1h | 148,350 | 2003-05-04 .. 2026-11-13 | 7,374 | 24 | 1e-05 | 1e-06 | 99% | 129 | — |
| `USD/CAD` | FX | sifting | 1h | 146,765 | 2003-08-03 .. 2026-11-13 | 7,290 | 24 | 1e-05 | 1e-06 | 99% | 129 | — |
| `USD/CHF` | FX | sifting | 1h | 148,239 | 2003-05-04 .. 2026-11-13 | 7,368 | 24 | 1e-05 | 1e-06 | 99% | 130 | — |
| `USD/CNH` | FX | sifting | 1h | 90,154 | 2012-06-26 .. 2026-11-13 | 4,462 | 24 | 1e-05 | 1e-06 | 99% | 84 | — |
| `USD/JPY` | FX | sifting | 1h | 148,244 | 2003-05-04 .. 2026-11-13 | 7,366 | 24 | 1e-05 | 1e-05 | 99% | 129 | — |
| `CC=F` | agriculture | yahoo | 1h | 16,517 | 2018-01-02 .. 2026-11-13 | 1,686 | 10 | 1 | 0.0001 | 0% | 739 | — |
| `CT=F` | agriculture | yahoo | 1h | 35,412 | 2018-01-02 .. 2026-11-13 | 1,992 | 18 | 0.01 | 1e-08 | 0% | 741 | — |
| `KC=F` | agriculture | yahoo | 1h | 17,459 | 2018-01-02 .. 2026-11-13 | 1,780 | 10 | 0.05 | 1e-07 | 0% | 738 | — |
| `LE=F` | agriculture | yahoo | 1h | 3,511 | 2024-05-10 .. 2026-11-13 | 589 | 6 | 0.025 | 1e-07 | 4% | 1122 | — |
| `SB=F` | agriculture | yahoo | 1h | 1,946 | 2026-02-03 .. 2026-11-13 | 198 | 10 | 0.01 | 1e-08 | 9% | 91 | — |
| `ZC=F` | agriculture | yahoo | 1h | 4,094 | 2026-01-02 .. 2026-11-13 | 219 | 19 | 0.25 | 0.01 | 4% | 82 | — |
| `ZS=F` | agriculture | yahoo | 1h | 31,053 | 2018-03-22 .. 2026-11-13 | 1,728 | 18 | 0.25 | 0.0001 | 0% | 595 | — |
| `ZW=F` | agriculture | yahoo | 1h | 3,732 | 2026-02-02 .. 2026-11-13 | 199 | 19 | 0.25 | 0.01 | 5% | 82 | — |
| `EMB` | credit | tiingo | 30min | 32,688 | 2007-12-19 .. 2026-11-13 | 4,751 | 7 | 0.01 | 1e-07 | 0% | 123 | — |
| `HYG` | credit | tiingo | 30min | 34,315 | 2007-04-11 .. 2026-11-13 | 4,932 | 7 | 0.01 | 1e-07 | 0% | 114 | — |
| `LQD` | credit | tiingo | 30min | 41,824 | 2002-12-30 .. 2026-11-13 | 6,003 | 7 | 0.01 | 1e-07 | 0% | 123 | — |
| `BTC/USDT` | crypto | binance | 1h | 120,575 | 2013-02-01 .. 2026-11-13 | 5,031 | 24 | 0.01 | 0.01 | 0% | 108 | — |
| `ETH/USDT` | crypto | binance | 1h | 92,683 | 2016-04-01 .. 2026-11-13 | 3,872 | 24 | 0.01 | 0.01 | 0% | 189 | — |
| `SOL/USDT` | crypto | binance | 1h | 54,837 | 2020-08-11 .. 2026-11-13 | 2,286 | 24 | 0.01 | 0.01 | 0% | 5 | — |
| `XRP/USDT` | crypto | binance | 1h | 84,928 | 2017-03-01 .. 2026-11-13 | 3,545 | 24 | 0.0001 | 0.0001 | 0% | 11 | — |
| `BZ=F` | energy | yahoo | 1h | 66,949 | 2011-01-02 .. 2026-11-13 | 3,270 | 21 | 0.01 | 1e-08 | 0% | 2215 | — |
| `TFM=F` | energy | marketwatch | 1h | 11,869 | 2022-01-03 .. 2026-11-13 | 1,244 | 10 | 0.005 | 0.001 | 100% | 116 | — |
| `UNG` | energy | twelvedata | 30min | 34,340 | 2007-04-18 .. 2026-11-13 | 4,926 | 7 | 0.01 | 1e-08 | 0% | 123 | — |
| `000300.SS` | equity | yahoo | 30min | 296 | 2026-08-14 .. 2026-11-13 | 60 | 5 | 0.01 | 1e-06 | 0% | 187 | volume present though not declared |
| `IWM` | equity | tiingo | 30min | 41,893 | 2002-12-30 .. 2026-11-13 | 6,005 | 7 | 0.01 | 1e-07 | 0% | 114 | — |
| `SPY` | equity | tiingo | 30min | 41,909 | 2002-12-30 .. 2026-11-13 | 6,007 | 7 | 0.01 | 1e-07 | 0% | 114 | — |
| `XLE` | equity | tiingo | 30min | 19,081 | 2016-01-04 .. 2026-11-13 | 2,732 | 7 | 0.01 | 1e-08 | 0% | 123 | — |
| `XLF` | equity | tiingo | 30min | 41,894 | 2002-12-30 .. 2026-11-13 | 6,005 | 7 | 0.01 | 1e-07 | 0% | 114 | — |
| `XLK` | equity | tiingo | 30min | 19,069 | 2016-01-04 .. 2026-11-13 | 2,732 | 7 | 0.01 | 1e-07 | 0% | 123 | — |
| `XLV` | equity | tiingo | 30min | 41,845 | 2002-12-30 .. 2026-11-13 | 6,004 | 7 | 0.01 | 1e-07 | 0% | 123 | — |
| `XU100.IS` | equity | yahoo | 30min | 546 | 2026-08-14 .. 2026-11-13 | 65 | 8 | 0.01 | 1e-05 | 100% | 65 | — |
| `^AXJO` | equity | yahoo | 30min | 3,691 | 2024-10-13 .. 2026-11-13 | 589 | 7 | 0.01 | 1e-06 | 100% | 116 | — |
| `^BVSP` | equity | yahoo | 30min | 3,614 | 2024-10-14 .. 2026-11-13 | 519 | 7 | 0.01 | 0.0001 | 100% | 117 | — |
| `^FTSE` | equity | yahoo | 30min | 4,731 | 2024-10-14 .. 2026-11-13 | 528 | 9 | 0.01 | 1e-06 | 100% | 116 | — |
| `^GSPTSE` | equity | yahoo | 30min | 448 | 2026-08-14 .. 2026-11-13 | 64 | 7 | 0.01 | 1e-05 | 0% | 90 | volume present though not declared |
| `^HSI` | equity | yahoo | 30min | 3,568 | 2024-10-14 .. 2026-11-13 | 514 | 7 | 0.01 | 1e-05 | 100% | 142 | — |
| `^J200.JO` | equity | yahoo | 30min | 4,138 | 2024-10-14 .. 2026-11-13 | 521 | 8 | 0.01 | 1e-05 | 100% | 118 | — |
| `^KS11` | equity | yahoo | 30min | 3,041 | 2024-10-14 .. 2026-11-13 | 508 | 6 | 0.01 | 1e-06 | 3% | 187 | volume present though not declared |
| `^MXX` | equity | yahoo | 30min | 448 | 2026-08-14 .. 2026-11-13 | 64 | 7 | 0.01 | 1e-05 | 2% | 91 | volume present though not declared |
| `^N225` | equity | yahoo | 30min | 3,513 | 2024-10-15 .. 2026-11-13 | 506 | 7 | 0.01 | 1e-05 | 100% | 162 | — |
| `^NSEI` | equity | yahoo | 15min | 3,130 | 2024-10-14 .. 2026-11-13 | 513 | 6 | 0.01 | 1e-05 | 100% | 91 | — |
| `^SSMI` | equity | yahoo | 30min | 4,682 | 2024-10-14 .. 2026-11-13 | 521 | 9 | 0.01 | 1e-05 | 100% | 136 | — |
| `^STOXX50E` | equity | yahoo | 30min | 4,676 | 2024-10-14 .. 2026-11-13 | 521 | 9 | 0.01 | 1e-06 | 100% | 140 | — |
| `^TASI.SR` | equity | yahoo | 30min | 2,587 | 2024-10-13 .. 2026-11-12 | 518 | 5 | 0.01 | 1e-06 | 12% | 236 | volume present though not declared |
| `^TWII` | equity | yahoo | 30min | 2,529 | 2024-10-14 .. 2026-11-13 | 506 | 5 | 0.01 | 1e-05 | 100% | 284 | — |
| `AHD` | industrial_metals | sina | 1h | 2,614 | 2026-04-22 .. 2026-11-13 | 160 | 18 | 0.5 | 0.001 | 40% | 79 | — |
| `CAD` | industrial_metals | sina | 1h | 2,604 | 2026-04-23 .. 2026-11-13 | 158 | 18 | 0.5 | 0.001 | 43% | 79 | — |
| `NID` | industrial_metals | sina | 1h | 2,620 | 2026-04-22 .. 2026-11-13 | 160 | 18 | 5 | 0.01 | 41% | 79 | — |
| `REMX` | industrial_metals | yahoo | 30min | 27,195 | 2010-10-28 .. 2026-11-13 | 4,033 | 7 | 0.01 | 1e-08 | 1% | 123 | — |
| `SND` | industrial_metals | sina | 1h | 2,440 | 2026-04-22 .. 2026-11-13 | 158 | 17 | 5 | 0.01 | 39% | 79 | — |
| `PL=F` | precious_metals | yahoo | 1h | 25,177 | 2021-11-01 .. 2026-11-13 | 1,315 | 23 | 0.1 | 1e-06 | 0% | 649 | — |
| `XAG/USD` | precious_metals | sifting | 1h | 141,785 | 2003-05-04 .. 2026-11-13 | 7,318 | 23 | 0.0001 | 0.0001 | 98% | 77 | volume present though not declared |
| `XAU/USD` | precious_metals | sifting | 1h | 142,781 | 2003-05-05 .. 2026-11-13 | 7,328 | 23 | 0.01 | 0.0001 | 98% | 77 | volume present though not declared |
| `511090.SS` | rates | yahoo | 30min | 4,144 | 2023-06-13 .. 2026-11-13 | 829 | 5 | 0.001 | 1e-07 | 93% | 259 | — |
| `FGBL=F` | rates | marketwatch | 1h | 30,207 | 2018-05-03 .. 2026-11-13 | 2,173 | 14 | 0.01 | 0.0001 | 100% | 131 | prices<=0: 1 |
| `IGLT.L` | rates | yahoo | 30min | 4,686 | 2024-10-21 .. 2026-11-13 | 523 | 9 | 0.0005 | 1e-09 | 9% | 116 | — |
| `TIP` | rates | tiingo | 30min | 40,102 | 2003-12-05 .. 2026-11-13 | 5,757 | 7 | 0.01 | 1e-07 | 0% | 312 | — |
| `ZB=F` | rates | yahoo | 1h | 39,104 | 2019-01-21 .. 2026-11-13 | 2,070 | 23 | 0.03125 | 1e-05 | 0% | 649 | — |
| `ZN=F` | rates | yahoo | 1h | 11,530 | 2024-10-13 .. 2026-11-13 | 609 | 23 | 0.015625 | 1e-06 | 4% | 87 | — |
| `ZT=F` | rates | yahoo | 1h | 11,601 | 2024-10-13 .. 2026-11-13 | 612 | 23 | 0.00390625 | 1e-07 | 4% | 87 | — |

## External stress indicator

`VIXCLS`: 9,312 daily values, 1990-01-02 .. 2026-11-06. Median publication lag — 20 h from midnight of the observation day (set in config/basket.yaml).

## Totals

Instruments: 62. Bars: 2,404,711. Rows with issues: 9.
