# MEALS backtest against the §7 yardstick

Scored window 2021-11-22 .. 2026-08-28 — from the hour the whole basket is warm (§6.6). Recall is per episode, not per hour, and the baselines carry the same 72-hour cooldown as the detector; see the module docstring for why.

## Detectors

| Detector | Alerts | Episodes | Caught | Precision | Recall | F1 | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|---:|
| SI-Index cluster events | 184 | 153 | 44 | 15.2% | 28.8% | 19.9% | 6 |
| SPY trailing 24h (runnable) | 65 | 153 | 28 | 35.4% | 18.3% | 24.1% | 16 |
| SPY trailing 24h (runnable), no cooldown | 940 | 153 | 60 | 48.4% | 39.2% | 43.3% | 21 |
| SPY forward 24h (§7 as written) | 65 | 153 | 47 | 53.8% | 30.7% | 39.1% | 3 |
| SPY forward 24h (§7 as written), no cooldown | 940 | 153 | 85 | 68.0% | 55.6% | 61.1% | 12 |

## Train and test (§7)

Calibration happens on train alone; test is reported so the gap is visible, not so it can be tuned against.

| Period | Alerts | Episodes | Caught | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| train | 87 | 111 | 34 | 25.3% | 30.6% | 27.7% |
| test | 97 | 42 | 10 | 6.2% | 23.8% | 9.8% |

## By block

| Block | Q99 of a 24h move | Episodes | Caught | Precision | Recall | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|
| FX | 0.0052 | 73 | 22 | 6.5% | 30.1% | 14 |
| commodities | 0.0222 | 37 | 9 | 3.3% | 24.3% | 1 |
| crypto | 0.1379 | 23 | 7 | 2.2% | 30.4% | 14 |
| equity | 0.0299 | 38 | 14 | 6.5% | 36.8% | 6 |
| rates | 0.0093 | 25 | 6 | 3.3% | 24.0% | 12 |

## Diagnostics (§7)

| Quantity | Value |
|---|---|
| Hours without quorum | 1.1% |
| Escalations | 68 |
| Hours suppressed by the cooldown | 102 |
| Early breaks refused by the debounce | 0 |
| csv_compression fires | 0.0% |
| pca_sync fires | 3.9% |
| Correlation of the two (§3.4, drop one above 0.7) | — |
| Calendar multiplier above 1, all hours | 59.0% |
| Calendar multiplier above 1, at events | 84.9% |
| Escalations inside a calendar window | 91.2% |
| Escalations the calendar multiplier decided | 75.0% |

Escalations by reason: `si>=escalation_threshold` 62, `breadth_q99` 6

Share of events on which each trigger was true: `cluster_shift` 100.0%, `price_shock` 95.1%, `single_factor` 16.8%, `volume` 30.8%

## Events by month

```
2021-11  # 1
2021-12  ### 3
2022-01  ### 3
2022-02  #### 4
2022-03  #### 4
2022-04  ##### 5
2022-05  ##### 5
2022-06  ### 3
2022-07  ### 3
2022-08  ##### 5
2022-09  #### 4
2022-10  ### 3
2022-11  ## 2
2022-12  ## 2
2023-01  ##### 5
2023-02  #### 4
2023-03  ### 3
2023-04  ### 3
2023-05  ### 3
2023-06  ### 3
2023-07  ## 2
2023-08  ### 3
2023-09  #### 4
2023-10  ##### 5
2023-11  ## 2
2023-12  ### 3
2024-01  ### 3
2024-02  # 1
2024-03  ## 2
2024-04  #### 4
2024-05  #### 4
2024-06  ### 3
2024-07  ### 3
2024-08  #### 4
2024-09  ## 2
2024-10  ### 3
2024-11  ## 2
2024-12  #### 4
2025-01  ### 3
2025-02  ## 2
2025-03  #### 4
2025-04  #### 4
2025-05  ### 3
2025-06  ### 3
2025-07  ### 3
2025-08  #### 4
2025-09  ##### 5
2025-10  ## 2
2025-11  ### 3
2025-12  #### 4
2026-01  ### 3
2026-02  # 1
2026-03  ##### 5
2026-04  ## 2
2026-05  #### 4
2026-06  ## 2
2026-07  ### 3
2026-08  ### 3
```
