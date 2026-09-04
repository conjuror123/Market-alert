# MEALS backtest against the §7 yardstick

Scored window 2021-11-22 .. 2026-08-28 — from the hour the whole basket is warm (§6.6). Recall is per episode, not per hour, and the baselines carry the same 72-hour cooldown as the detector; see the module docstring for why.

## Detectors

| Detector | Alerts | Episodes | Caught | Precision | Recall | F1 | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|---:|
| SI-Index cluster events | 189 | 153 | 40 | 14.8% | 26.1% | 18.9% | 6 |
| SPY trailing 24h (runnable) | 65 | 153 | 28 | 35.4% | 18.3% | 24.1% | 16 |
| SPY trailing 24h (runnable), no cooldown | 940 | 153 | 60 | 48.4% | 39.2% | 43.3% | 21 |
| SPY forward 24h (§7 as written) | 65 | 153 | 47 | 53.8% | 30.7% | 39.1% | 3 |
| SPY forward 24h (§7 as written), no cooldown | 940 | 153 | 85 | 68.0% | 55.6% | 61.1% | 12 |

## Train and test (§7)

Calibration happens on train alone; test is reported so the gap is visible, not so it can be tuned against.

| Period | Alerts | Episodes | Caught | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| train | 89 | 111 | 32 | 25.8% | 28.8% | 27.3% |
| test | 100 | 42 | 8 | 5.0% | 19.0% | 7.9% |

## By block

| Block | Q99 of a 24h move | Episodes | Caught | Precision | Recall | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|
| FX | 0.0052 | 73 | 15 | 5.3% | 20.5% | 13 |
| commodities | 0.0222 | 37 | 8 | 2.6% | 21.6% | 1 |
| crypto | 0.1379 | 23 | 5 | 1.6% | 21.7% | 14 |
| equity | 0.0299 | 38 | 13 | 5.8% | 34.2% | 5 |
| rates | 0.0093 | 25 | 7 | 3.7% | 28.0% | 3 |

## Diagnostics (§7)

| Quantity | Value |
|---|---|
| Hours without quorum | 1.1% |
| Escalations | 88 |
| Hours suppressed by the cooldown | 125 |
| Early breaks refused by the debounce | 0 |
| csv_compression fires | 0.0% |
| pca_sync fires | 3.9% |
| Correlation of the two (§3.4, drop one above 0.7) | — |
| Calendar multiplier above 1, all hours | 17.2% |
| Calendar multiplier above 1, at events | 40.5% |
| Escalations inside a calendar window | 63.6% |
| Escalations the calendar multiplier decided | 39.8% |

Escalations by reason: `si>=escalation_threshold` 88

Share of events on which each trigger was true: `cluster_shift` 100.0%, `price_shock` 85.8%, `single_factor` 16.8%, `volume` 26.3%

## Events by month

```
2021-11  # 1
2021-12  #### 4
2022-01  #### 4
2022-02  ### 3
2022-03  #### 4
2022-04  ##### 5
2022-05  ##### 5
2022-06  ### 3
2022-07  ### 3
2022-08  #### 4
2022-09  #### 4
2022-10  #### 4
2022-11  ## 2
2022-12  ## 2
2023-01  ##### 5
2023-02  ##### 5
2023-03  ### 3
2023-04  ### 3
2023-05  ### 3
2023-06  ### 3
2023-07  ## 2
2023-08  ### 3
2023-09  #### 4
2023-10  #### 4
2023-11  ### 3
2023-12  ### 3
2024-01  ### 3
2024-02  ## 2
2024-03  ## 2
2024-04  #### 4
2024-05  ### 3
2024-06  ### 3
2024-07  #### 4
2024-08  #### 4
2024-09  ### 3
2024-10  ## 2
2024-11  ## 2
2024-12  #### 4
2025-01  #### 4
2025-02  ### 3
2025-03  ### 3
2025-04  #### 4
2025-05  #### 4
2025-06  ### 3
2025-07  ### 3
2025-08  ### 3
2025-09  #### 4
2025-10  ## 2
2025-11  ### 3
2025-12  ### 3
2026-01  ### 3
2026-02  # 1
2026-03  ##### 5
2026-04  ### 3
2026-05  #### 4
2026-06  ### 3
2026-07  #### 4
2026-08  ### 3
```
