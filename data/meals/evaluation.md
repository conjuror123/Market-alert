# MEALS backtest against the §7 yardstick

Scored window 2021-11-22 .. 2026-08-28 — from the hour the whole basket is warm (§6.6). Recall is per episode, not per hour, and the baselines carry the same 72-hour cooldown as the detector; see the module docstring for why.

## Detectors

| Detector | Alerts | Episodes | Caught | Precision | Recall | F1 | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|---:|
| SI-Index cluster events | 118 | 153 | 37 | 22.0% | 24.2% | 23.1% | 3 |
| SPY trailing 24h (runnable) | 65 | 153 | 28 | 35.4% | 18.3% | 24.1% | 16 |
| SPY trailing 24h (runnable), no cooldown | 940 | 153 | 60 | 48.4% | 39.2% | 43.3% | 21 |
| SPY forward 24h (§7 as written) | 65 | 153 | 47 | 53.8% | 30.7% | 39.1% | 3 |
| SPY forward 24h (§7 as written), no cooldown | 940 | 153 | 85 | 68.0% | 55.6% | 61.1% | 12 |

## Train and test (§7)

Calibration happens on train alone; test is reported so the gap is visible, not so it can be tuned against.

| Period | Alerts | Episodes | Caught | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| train | 59 | 111 | 30 | 37.3% | 27.0% | 31.3% |
| test | 59 | 42 | 7 | 6.8% | 16.7% | 9.6% |

## By block

| Block | Q99 of a 24h move | Episodes | Caught | Precision | Recall | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|
| FX | 0.0052 | 73 | 13 | 6.8% | 17.8% | 10 |
| commodities | 0.0222 | 37 | 8 | 4.2% | 21.6% | 0 |
| crypto | 0.1379 | 23 | 6 | 4.2% | 26.1% | 6 |
| equity | 0.0299 | 38 | 15 | 9.3% | 39.5% | 3 |
| rates | 0.0093 | 25 | 6 | 5.1% | 24.0% | 2 |

## Diagnostics (§7)

| Quantity | Value |
|---|---|
| Hours without quorum | 1.1% |
| Escalations | 7 |
| Hours suppressed by the cooldown | 43 |
| Early breaks refused by the debounce | 0 |
| basket_coherence fires | 0.2% |
| pca_sync fires | 3.9% |
| Correlation of the two (§3.4, drop one above 0.7) | 0.067 |
| csv_compression fires (retired, §21) | 0.0% |
| Calendar multiplier above 1, all hours | 17.2% |
| Calendar multiplier above 1, at events | 67.2% |
| Escalations inside a calendar window | 85.7% |
| Escalations the calendar multiplier decided | 85.7% |

Escalations by reason: `si>=escalation_threshold` 6, `breadth_q99` 1

Share of events on which each trigger was true: `cluster_shift` 100.0%, `price_shock` 83.2%, `single_factor` 23.5%, `volume` 42.0%

## Events by month

```
2021-11  # 1
2021-12  # 1
2022-01  #### 4
2022-02  #### 4
2022-03  ## 2
2022-04  # 1
2022-05  #### 4
2022-06  ## 2
2022-07  ### 3
2022-08  #### 4
2022-09  ### 3
2022-10  #### 4
2022-11  #### 4
2022-12  # 1
2023-01  ## 2
2023-02  ### 3
2023-03  ## 2
2023-04  ## 2
2023-05  ## 2
2023-07  # 1
2023-08  ## 2
2023-09  # 1
2023-10  ### 3
2023-12  ### 3
2024-01  ## 2
2024-02  # 1
2024-03  # 1
2024-04  ## 2
2024-05  ### 3
2024-06  # 1
2024-08  ## 2
2024-09  # 1
2024-10  ## 2
2024-11  # 1
2024-12  ### 3
2025-01  ## 2
2025-02  ## 2
2025-03  # 1
2025-04  #### 4
2025-05  ## 2
2025-06  # 1
2025-07  # 1
2025-08  ## 2
2025-09  ### 3
2025-10  ## 2
2025-11  ## 2
2025-12  ## 2
2026-01  ## 2
2026-03  ##### 5
2026-04  # 1
2026-05  # 1
2026-06  ### 3
2026-07  ### 3
2026-08  ## 2
```
