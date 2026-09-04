# MEALS backtest against the §7 yardstick

Scored window 2021-11-22 .. 2026-08-28 — from the hour the whole basket is warm (§6.6). Recall is per episode, not per hour, and the baselines carry the same 72-hour cooldown as the detector; see the module docstring for why.

## Detectors

| Detector | Alerts | Episodes | Caught | Precision | Recall | F1 | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|---:|
| SI-Index cluster events | 141 | 180 | 49 | 19.9% | 27.2% | 23.0% | 6 |
| SPY trailing 24h (runnable) | 65 | 180 | 34 | 36.9% | 18.9% | 25.0% | 16 |
| SPY trailing 24h (runnable), no cooldown | 940 | 180 | 73 | 51.7% | 40.6% | 45.5% | 22 |
| SPY forward 24h (§7 as written) | 65 | 180 | 49 | 56.9% | 27.2% | 36.8% | 1 |
| SPY forward 24h (§7 as written), no cooldown | 940 | 180 | 91 | 71.1% | 50.6% | 59.1% | 12 |

## Against a softened label

§7 cuts truth at a block's 99th percentile, and an alert before a move reaching 0.99 of that line scores as a total failure. The same detector, same alerts, against a label at 0.75 of the threshold:

| Label | Episodes | Caught | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|
| §7 as written | 180 | 49 | 19.9% | 27.2% | 23.0% |
| at 0.75 of the threshold | 435 | 92 | 42.6% | 21.1% | 28.3% |

## Train and test (§7)

Calibration happens on train alone; test is reported so the gap is visible, not so it can be tuned against.

| Period | Alerts | Episodes | Caught | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| train | 91 | 148 | 43 | 27.5% | 29.1% | 28.2% |
| test | 50 | 32 | 6 | 6.0% | 18.8% | 9.1% |

## By block

| Block | Q99 of a 24h move | Episodes | Caught | Precision | Recall | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|
| FX | 0.0050 | 89 | 24 | 8.5% | 27.0% | 13 |
| commodities | 0.0213 | 45 | 11 | 5.7% | 24.4% | 0 |
| crypto | 0.1296 | 27 | 7 | 4.3% | 25.9% | 12 |
| equity | 0.0290 | 43 | 11 | 5.7% | 25.6% | 3 |
| rates | 0.0088 | 28 | 8 | 5.7% | 28.6% | 2 |

## Diagnostics (§7)

| Quantity | Value |
|---|---|
| Hours without quorum | 1.1% |
| Escalations | 35 |
| Hours suppressed by the cooldown | 73 |
| Early breaks refused by the debounce | 0 |
| basket_coherence fires | 0.2% |
| pca_sync fires | 3.9% |
| Correlation of the two (§3.4, drop one above 0.7) | 0.067 |
| csv_compression fires (retired, §21) | 0.0% |
| Calendar multiplier above 1, all hours | 17.2% |
| Calendar multiplier above 1, at events | 54.2% |
| Escalations inside a calendar window | 82.9% |
| Escalations the calendar multiplier decided | 82.9% |

Escalations by reason: `si>=escalation_threshold` 35

Share of events on which each trigger was true: `cluster_shift` 100.0%, `price_shock` 41.5%, `saed_breadth` 8.5%, `single_factor` 19.7%, `sustained` 0.0%, `volume` 15.5%

## Events by month

```
2021-11  # 1
2021-12  # 1
2022-01  ### 3
2022-02  ### 3
2022-03  ### 3
2022-04  #### 4
2022-05  #### 4
2022-06  ## 2
2022-07  ### 3
2022-08  #### 4
2022-09  ### 3
2022-10  #### 4
2022-11  #### 4
2022-12  # 1
2023-01  ### 3
2023-02  ##### 5
2023-03  ### 3
2023-04  ## 2
2023-05  ### 3
2023-07  # 1
2023-08  # 1
2023-09  ### 3
2023-10  ### 3
2023-11  ## 2
2023-12  ## 2
2024-01  ## 2
2024-02  # 1
2024-03  # 1
2024-04  ### 3
2024-05  ### 3
2024-06  # 1
2024-07  # 1
2024-08  ## 2
2024-09  ## 2
2024-10  ## 2
2024-11  ## 2
2024-12  ### 3
2025-01  ### 3
2025-02  ## 2
2025-03  ## 2
2025-04  #### 4
2025-05  ## 2
2025-06  ### 3
2025-07  ### 3
2025-08  ## 2
2025-09  ## 2
2025-10  ## 2
2025-11  ## 2
2025-12  ### 3
2026-01  ## 2
2026-02  # 1
2026-03  ##### 5
2026-04  # 1
2026-05  ### 3
2026-06  #### 4
2026-07  ### 3
2026-08  ## 2
```
