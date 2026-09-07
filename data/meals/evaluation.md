# MEALS backtest against the §7 yardstick

Scored window 2021-11-14 .. 2026-09-03 — from the hour the whole basket is warm (§6.6). Recall is per episode, not per hour, and the baselines carry the same 72-hour cooldown as the detector; see the module docstring for why.

## Detectors

| Detector | Alerts | Episodes | Caught | Precision | Recall | F1 | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|---:|
| SI-Index cluster events | 139 | 130 | 32 | 18.0% | 24.6% | 20.8% | 4 |
| SPY trailing 24h (runnable) | 65 | 130 | 23 | 27.7% | 17.7% | 21.6% | 12 |
| SPY trailing 24h (runnable), no cooldown | 940 | 130 | 52 | 33.2% | 40.0% | 36.3% | 23 |
| SPY forward 24h (§7 as written) | 65 | 130 | 30 | 30.8% | 23.1% | 26.4% | 14 |
| SPY forward 24h (§7 as written), no cooldown | 940 | 130 | 58 | 41.6% | 44.6% | 43.1% | 16 |

## Against a softened label

§7 cuts truth at a block's 99th percentile, and an alert before a move reaching 0.99 of that line scores as a total failure. The same detector, same alerts, against a label at 0.75 of the threshold:

| Label | Episodes | Caught | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|
| §7 as written | 130 | 32 | 18.0% | 24.6% | 20.8% |
| at 0.75 of the threshold | 342 | 64 | 28.8% | 18.7% | 22.7% |

## Train and test (§7)

Calibration happens on train alone; test is reported so the gap is visible, not so it can be tuned against.

| Period | Alerts | Episodes | Caught | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| train | 91 | 100 | 25 | 23.1% | 25.0% | 24.0% |
| test | 48 | 30 | 7 | 8.3% | 23.3% | 12.3% |

## By block

| Block | Q99 of a 24h move | Episodes | Caught | Precision | Recall | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|
| FX | 0.0071 | 21 | 3 | 1.4% | 14.3% | 10 |
| commodities | 0.0244 | 26 | 8 | 3.6% | 30.8% | 0 |
| crypto | 0.1157 | 46 | 6 | 4.3% | 13.0% | 14 |
| equity | 0.0357 | 16 | 7 | 3.6% | 43.8% | 4 |
| rates | 0.0080 | 41 | 12 | 8.6% | 29.3% | 2 |

## Diagnostics (§7)

| Quantity | Value |
|---|---|
| Hours without quorum | 1.5% |
| Escalations | 28 |
| Hours suppressed by the cooldown | 63 |
| Early breaks refused by the debounce | 0 |
| basket_coherence fires | 0.1% |
| pca_sync fires | 3.8% |
| Correlation of the two (§3.4, drop one above 0.7) | -0.019 |
| csv_compression fires (retired, §21) | 0.0% |
| Calendar multiplier above 1, all hours | 17.2% |
| Calendar multiplier above 1, at events | 53.2% |
| Escalations inside a calendar window | 71.4% |
| Escalations the calendar multiplier decided | 64.3% |

Escalations by reason: `si>=escalation_threshold` 28

Share of events on which each trigger was true: `cluster_shift` 100.0%, `price_shock` 41.0%, `saed_breadth` 8.6%, `single_factor` 20.1%, `sustained` 0.0%, `volume` 14.4%

## Events by month

```
2021-11  # 1
2021-12  # 1
2022-01  ### 3
2022-02  ### 3
2022-03  ### 3
2022-04  ### 3
2022-05  ### 3
2022-06  ## 2
2022-07  ### 3
2022-08  #### 4
2022-09  ### 3
2022-10  #### 4
2022-11  ##### 5
2022-12  # 1
2023-01  ### 3
2023-02  ##### 5
2023-03  ### 3
2023-04  ## 2
2023-05  ### 3
2023-07  # 1
2023-08  # 1
2023-09  #### 4
2023-10  ### 3
2023-11  # 1
2023-12  ### 3
2024-01  ## 2
2024-02  # 1
2024-03  # 1
2024-04  ### 3
2024-05  ### 3
2024-06  # 1
2024-08  ## 2
2024-09  ### 3
2024-10  ## 2
2024-11  ## 2
2024-12  ### 3
2025-01  ### 3
2025-03  ## 2
2025-04  #### 4
2025-05  ## 2
2025-06  ### 3
2025-07  ### 3
2025-08  ## 2
2025-09  ## 2
2025-10  # 1
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
