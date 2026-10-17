# Jump data coverage

Generated 2026-10-17 17:03 UTC by `python -m jump.audit`. One row per instrument: its provider, how far back its bars reach, its hourly volume, and anything wrong with the series (Notes; `—` is sound). Periods end where the bars on disk end: without the open months (`tools/hot_bars.sh restore`), at the last settled month.

## Basket

| Instrument | Block | Provider | Interval | Bars | Period | Days | Bars per day | Price step | Source precision | Volume=0 | Max gap, h | Notes |
|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---|
| `AUD/USD` | FX | sifting | 1h | 145,250 | 2003-08-03 .. 2026-09-30 | 7,254 | 24 | 1e-05 | 1e-06 | 100% | 99 | — |
| `EUR/USD` | FX | sifting | 1h | 147,534 | 2003-05-04 .. 2026-09-30 | 7,337 | 24 | 1e-05 | 1e-06 | 100% | 129 | OHLC: 12 |
| `GBP/USD` | FX | sifting | 1h | 147,588 | 2003-05-04 .. 2026-09-30 | 7,337 | 24 | 1e-05 | 1e-06 | 100% | 129 | — |
| `USD/CAD` | FX | sifting | 1h | 146,003 | 2003-08-03 .. 2026-09-30 | 7,253 | 24 | 1e-05 | 1e-06 | 100% | 129 | — |
| `USD/CHF` | FX | sifting | 1h | 147,476 | 2003-05-04 .. 2026-09-30 | 7,330 | 24 | 1e-05 | 1e-06 | 100% | 130 | — |
| `USD/CNH` | FX | sifting | 1h | 89,389 | 2012-06-26 .. 2026-09-30 | 4,424 | 24 | 1e-05 | 1e-06 | 100% | 84 | — |
| `USD/JPY` | FX | sifting | 1h | 147,482 | 2003-05-04 .. 2026-09-30 | 7,329 | 24 | 1e-05 | 1e-05 | 100% | 129 | — |
| `CC=F` | agriculture | yahoo | 1h | 16,203 | 2018-01-02 .. 2026-09-30 | 1,654 | 10 | 1 | 0.0001 | 0% | 739 | — |
| `CT=F` | agriculture | yahoo | 1h | 34,836 | 2018-01-02 .. 2026-09-30 | 1,960 | 18 | 0.01 | 1e-08 | 0% | 741 | — |
| `KC=F` | agriculture | yahoo | 1h | 17,145 | 2018-01-02 .. 2026-09-30 | 1,748 | 10 | 0.05 | 1e-07 | 0% | 738 | — |
| `LE=F` | agriculture | yahoo | 1h | 3,320 | 2024-05-10 .. 2026-09-30 | 557 | 6 | 0.025 | 1e-07 | 3% | 1122 | — |
| `SB=F` | agriculture | yahoo | 1h | 1,630 | 2026-02-03 .. 2026-09-30 | 166 | 10 | 0.01 | 1e-08 | 8% | 91 | — |
| `ZC=F` | agriculture | yahoo | 1h | 3,486 | 2026-01-02 .. 2026-09-30 | 187 | 19 | 0.25 | 0.01 | 4% | 82 | — |
| `ZS=F` | agriculture | yahoo | 1h | 30,445 | 2018-03-22 .. 2026-09-30 | 1,696 | 18 | 0.25 | 0.0001 | 0% | 595 | — |
| `ZW=F` | agriculture | yahoo | 1h | 3,124 | 2026-02-02 .. 2026-09-30 | 167 | 19 | 0.25 | 0.01 | 5% | 82 | — |
| `EMB` | credit | tiingo | 30min | 32,464 | 2007-12-19 .. 2026-09-30 | 4,719 | 7 | 0.01 | 1e-07 | 0% | 123 | — |
| `HYG` | credit | tiingo | 30min | 34,091 | 2007-04-11 .. 2026-09-30 | 4,900 | 7 | 0.01 | 1e-07 | 0% | 114 | — |
| `LQD` | credit | tiingo | 30min | 41,600 | 2002-12-30 .. 2026-09-30 | 5,971 | 7 | 0.01 | 1e-07 | 0% | 123 | — |
| `BTC/USDT` | crypto | binance | 1h | 119,520 | 2013-02-01 .. 2026-09-30 | 4,987 | 24 | 0.01 | 0.01 | 0% | 108 | — |
| `ETH/USDT` | crypto | binance | 1h | 91,628 | 2016-04-01 .. 2026-09-30 | 3,828 | 24 | 0.01 | 0.01 | 0% | 189 | — |
| `SOL/USDT` | crypto | binance | 1h | 53,782 | 2020-08-11 .. 2026-09-30 | 2,242 | 24 | 0.01 | 0.01 | 0% | 5 | — |
| `XRP/USDT` | crypto | binance | 1h | 83,873 | 2017-03-01 .. 2026-09-30 | 3,501 | 24 | 0.0001 | 0.0001 | 0% | 11 | — |
| `BZ=F` | energy | yahoo | 1h | 66,215 | 2011-01-02 .. 2026-09-30 | 3,232 | 21 | 0.01 | 1e-08 | 0% | 2215 | — |
| `TFM=F` | energy | marketwatch | 1h | 20 | 2026-09-29 .. 2026-09-30 | 2 | 10 | 0.005 | 0.001 | 100% | 15 | — |
| `UNG` | energy | twelvedata | 30min | 34,116 | 2007-04-18 .. 2026-09-30 | 4,894 | 7 | 0.01 | 1e-08 | 0% | 123 | — |
| `000300.SS` | equity | yahoo | 30min | 161 | 2026-08-14 .. 2026-09-30 | 33 | 5 | 0.01 | 1e-06 | 0% | 91 | volume present though not declared |
| `IWM` | equity | tiingo | 30min | 41,669 | 2002-12-30 .. 2026-09-30 | 5,973 | 7 | 0.01 | 1e-07 | 0% | 114 | — |
| `SPY` | equity | tiingo | 30min | 41,685 | 2002-12-30 .. 2026-09-30 | 5,975 | 7 | 0.01 | 1e-07 | 0% | 114 | — |
| `XLE` | equity | tiingo | 30min | 18,857 | 2016-01-04 .. 2026-09-30 | 2,700 | 7 | 0.01 | 1e-08 | 0% | 123 | — |
| `XLF` | equity | tiingo | 30min | 41,670 | 2002-12-30 .. 2026-09-30 | 5,973 | 7 | 0.01 | 1e-07 | 0% | 114 | — |
| `XLK` | equity | tiingo | 30min | 18,845 | 2016-01-04 .. 2026-09-30 | 2,700 | 7 | 0.01 | 1e-07 | 0% | 123 | — |
| `XLV` | equity | tiingo | 30min | 41,621 | 2002-12-30 .. 2026-09-30 | 5,972 | 7 | 0.01 | 1e-07 | 0% | 123 | — |
| `XU100.IS` | equity | yahoo | 30min | 272 | 2026-08-14 .. 2026-09-30 | 34 | 8 | 0.01 | 1e-05 | 100% | 65 | — |
| `^AXJO` | equity | yahoo | 30min | 3,467 | 2024-10-13 .. 2026-09-30 | 551 | 7 | 0.01 | 1e-06 | 100% | 116 | — |
| `^BVSP` | equity | yahoo | 30min | 3,404 | 2024-10-14 .. 2026-09-30 | 489 | 7 | 0.01 | 0.0001 | 100% | 117 | — |
| `^FTSE` | equity | yahoo | 30min | 4,443 | 2024-10-14 .. 2026-09-30 | 496 | 9 | 0.01 | 1e-06 | 100% | 116 | — |
| `^GSPTSE` | equity | yahoo | 30min | 231 | 2026-08-14 .. 2026-09-30 | 33 | 7 | 0.01 | 1e-05 | 0% | 90 | volume present though not declared |
| `^HSI` | equity | yahoo | 30min | 3,357 | 2024-10-14 .. 2026-09-30 | 484 | 7 | 0.01 | 1e-05 | 100% | 142 | — |
| `^J200.JO` | equity | yahoo | 30min | 3,890 | 2024-10-14 .. 2026-09-30 | 490 | 8 | 0.01 | 1e-05 | 100% | 118 | — |
| `^KS11` | equity | yahoo | 30min | 2,861 | 2024-10-14 .. 2026-09-30 | 478 | 6 | 0.01 | 1e-06 | 4% | 187 | volume present though not declared |
| `^MXX` | equity | yahoo | 30min | 231 | 2026-08-14 .. 2026-09-30 | 33 | 7 | 0.01 | 1e-05 | 0% | 66 | volume present though not declared |
| `^N225` | equity | yahoo | 30min | 3,303 | 2024-10-15 .. 2026-09-30 | 476 | 7 | 0.01 | 1e-05 | 100% | 162 | — |
| `^NSEI` | equity | yahoo | 15min | 2,927 | 2024-10-14 .. 2026-09-30 | 484 | 6 | 0.01 | 1e-05 | 100% | 91 | — |
| `^SSMI` | equity | yahoo | 30min | 4,394 | 2024-10-14 .. 2026-09-30 | 489 | 9 | 0.01 | 1e-05 | 100% | 136 | — |
| `^STOXX50E` | equity | yahoo | 30min | 4,388 | 2024-10-14 .. 2026-09-30 | 489 | 9 | 0.01 | 1e-06 | 100% | 140 | — |
| `^TASI.SR` | equity | yahoo | 30min | 2,432 | 2024-10-13 .. 2026-09-30 | 487 | 5 | 0.01 | 1e-05 | 12% | 236 | volume present though not declared |
| `^TWII` | equity | yahoo | 30min | 2,384 | 2024-10-14 .. 2026-09-30 | 477 | 5 | 0.01 | 1e-05 | 100% | 284 | — |
| `AHD` | industrial_metals | sina | 1h | 2,031 | 2026-04-22 .. 2026-09-30 | 122 | 18 | 0.5 | 0.001 | 52% | 79 | — |
| `CAD` | industrial_metals | sina | 1h | 2,022 | 2026-04-23 .. 2026-09-30 | 120 | 18 | 0.5 | 0.001 | 56% | 79 | — |
| `NID` | industrial_metals | sina | 1h | 2,038 | 2026-04-22 .. 2026-09-30 | 122 | 18 | 5 | 0.01 | 53% | 79 | — |
| `REMX` | industrial_metals | yahoo | 30min | 26,971 | 2010-10-28 .. 2026-09-30 | 4,001 | 7 | 0.01 | 1e-08 | 1% | 123 | — |
| `SND` | industrial_metals | sina | 1h | 1,909 | 2026-04-22 .. 2026-09-30 | 120 | 17 | 5 | 0.01 | 49% | 79 | — |
| `PL=F` | precious_metals | yahoo | 1h | 24,442 | 2021-11-01 .. 2026-09-30 | 1,277 | 23 | 0.1 | 1e-06 | 0% | 649 | — |
| `XAG/USD` | precious_metals | sifting | 1h | 141,020 | 2003-05-04 .. 2026-09-30 | 7,280 | 23 | 0.0001 | 0.0001 | 98% | 77 | volume present though not declared |
| `XAU/USD` | precious_metals | sifting | 1h | 142,016 | 2003-05-05 .. 2026-09-30 | 7,290 | 23 | 0.01 | 0.0001 | 98% | 77 | volume present though not declared |
| `FGBL=F` | rates | marketwatch | 1h | 29,887 | 2018-05-03 .. 2026-09-30 | 2,141 | 14 | 0.01 | 0.0001 | 100% | 131 | — |
| `JP10Y` | rates | marketwatch | 1h | 9 | 2026-09-30 .. 2026-09-30 | 1 | 9 | 0.0001 | 0.0001 | 100% | 1 | — |
| `RLI=F` | rates | marketwatch | 1h | 21,165 | 2018-05-01 .. 2026-09-30 | 2,123 | 10 | 0.01 | 0.0001 | 100% | 116 | — |
| `TIP` | rates | tiingo | 30min | 39,878 | 2003-12-05 .. 2026-09-30 | 5,725 | 7 | 0.01 | 1e-07 | 0% | 312 | — |
| `ZB=F` | rates | yahoo | 1h | 38,370 | 2019-01-21 .. 2026-09-30 | 2,032 | 23 | 0.03125 | 1e-05 | 0% | 649 | — |
| `ZN=F` | rates | yahoo | 1h | 10,795 | 2024-10-13 .. 2026-09-30 | 571 | 23 | 0.015625 | 1e-06 | 4% | 87 | — |
| `ZT=F` | rates | yahoo | 1h | 10,864 | 2024-10-13 .. 2026-09-30 | 574 | 23 | 0.00390625 | 1e-07 | 4% | 87 | — |

## External stress indicator

`VIXCLS`: 9,297 daily values, 1990-01-02 .. 2026-10-16. Median publication lag — 20 h from midnight of the observation day (set in config/basket.yaml).

## Totals

Instruments: 62. Bars: 2,378,129. Rows with issues: 8.
