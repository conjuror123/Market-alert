# Tremor backtest

> **Frozen artifact.** Do not regenerate this file with `python -m tremor.evaluate`.
> Delivery no longer states a frequency (`price_monitor/tremor_delivery.py`);
> `tremor.saed_score` no longer scores that claim. The 0.45× / 1.75× table
> below records what the old wording promised. See `docs/decisions.md`.
> `python -m tremor.evaluate` refuses this path unless `--force` is passed.

## The detector that is delivered

`saed_events.parquet` — 8,838 events, which is what `price_monitor` reads and what reaches a phone. The SI-Index table further down scores a different channel that is written and delivered to nobody; the two are not comparable and the numbers below are the ones that describe the product.

### Does a tier fire as often as it claims?

Every message states a return period. That is a falsifiable claim about frequency and this is the test of it. Levels are fitted on an expanding window and applied forward only, so the realised rate is out of sample.

| Tier | Says | Actually | Instruments | Instrument-years | Events | Ratio |
|---|---|---|---:|---:|---:|---:|
| noticeable | about once a month | about once in 2 months | 70 | 968 | 5247 | 0.45× |
| high | about once a quarter | about once a quarter | 70 | 968 | 2970 | 0.88× |
| major | about once in 3 years | about once in 2 years | 70 | 893 | 419 | 1.41× |
| extreme | about once in 6 years | about once in 3 years | 65 | 692 | 202 | 1.75× |

A ratio above 1 means the tier fires more often than its words promise, and the message overstates the rarity by that factor.

**Tiers no level was ever fitted for**, which are silent by construction rather than because the market was quiet:

- `extreme`: 5 instruments — ADA-USD, AVAX-USD, DOGE-USD, SOL-USD, XLP

### Were the moves it sent actually rare?

Each event judged against a plain full-sample quantile of the quantity ITS OWN ladder scores — `absolute` against the raw return, `abnormal` against the standardised residual — drawn at the rate its tier claims. Swapping the two yardsticks scores them at 3.5% and 1.1% and means nothing except that they were swapped.

| Basis | Tier | Events | On target | Precision |
|---|---|---:|---:|---:|
| abnormal | extreme | 54 | 28 | 51.9% |
| abnormal | high | 1258 | 1016 | 80.8% |
| abnormal | major | 86 | 48 | 55.8% |
| abnormal | noticeable | 2089 | 1831 | 87.6% |
| absolute | extreme | 42 | 11 | 26.2% |
| absolute | high | 1282 | 710 | 55.4% |
| absolute | major | 209 | 40 | 19.1% |
| absolute | noticeable | 2918 | 2288 | 78.4% |
| both | extreme | 81 | 42 | 51.9% |
| both | high | 430 | 362 | 84.2% |
| both | major | 86 | 34 | 39.5% |
| both | noticeable | 240 | 236 | 98.3% |

### What did it miss?

Per episode, not per hour: one shock spans several bars and the detector reports the peak and suppresses the repeats, so a per-hour figure would mostly measure the debounce.

| Label | Tier | Episodes | Caught | Recall |
|---|---|---:|---:|---:|
| every large move | noticeable | 8932 | 4998 | 56.0% |
| every large move | high | 2587 | 1773 | 68.5% |
| every large move | major | 292 | 187 | 64.0% |
| every large move | extreme | 142 | 90 | 63.4% |
| large and unexplained | noticeable | 1167 | 991 | 84.9% |
| large and unexplained | high | 252 | 218 | 86.5% |
| large and unexplained | major | 25 | 17 | 68.0% |
| large and unexplained | extreme | 11 | 8 | 72.7% |

`every large move` counts the moves a block fully explains, which are meant to be silent — twenty members of one complex moving together is one observation, and firing twenty times about it is the fault this system was built to fix. So that row is a floor. `large and unexplained` is the one to read.


---

# Against the §7 yardstick

Scored window 2023-06-21 .. 2026-09-10 — from the hour the whole basket is warm (§6.6). Recall is per episode, not per hour, and the baselines carry the same 72-hour cooldown as the detector; see the module docstring for why.

THE DETECTOR SCORED BELOW IS NOT THE ONE DELIVERED. It is the SI-Index cluster channel; `price_monitor` reads `saed_events.parquet`. The §7 label also asks a forecasting question — did a big move follow in the next 24 hours — which is not what either detector claims to answer. Both tables are kept because the comparison against the SPY baseline is worth having; neither is a verdict on the product.

## Detectors

| Detector | Alerts | Episodes | Caught | Precision | Recall | F1 | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|---:|
| SI-Index cluster events | 77 | 91 | 14 | 13.0% | 15.4% | 14.1% | 12 |
| SPY trailing 24h (runnable) | 19 | 91 | 11 | 42.1% | 12.1% | 18.8% | 16 |
| SPY trailing 24h (runnable), no cooldown | 295 | 91 | 17 | 59.0% | 18.7% | 28.4% | 20 |
| SPY forward 24h (§7 as written) | 19 | 91 | 8 | 36.8% | 8.8% | 14.2% | -0 |
| SPY forward 24h (§7 as written), no cooldown | 295 | 91 | 21 | 68.8% | 23.1% | 34.6% | 18 |

## Against a softened label

§7 cuts truth at a block's 99th percentile, and an alert before a move reaching 0.99 of that line scores as a total failure. The same detector, same alerts, against a label at 0.75 of the threshold:

| Label | Episodes | Caught | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|
| §7 as written | 91 | 14 | 13.0% | 15.4% | 14.1% |
| at 0.75 of the threshold | 217 | 29 | 31.2% | 13.4% | 18.7% |

## Train and test (§7)

Calibration happens on train alone; test is reported so the gap is visible, not so it can be tuned against.

| Period | Alerts | Episodes | Caught | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| train | 31 | 24 | 3 | 6.5% | 12.5% | 8.5% |
| test | 46 | 67 | 11 | 17.4% | 16.4% | 16.9% |

## By block

| Block | Q99 of a 24h move | Episodes | Caught | Precision | Recall | Median lead, h |
|---|---:|---:|---:|---:|---:|---:|
| FX | 0.0103 | 4 | 3 | 1.3% | 75.0% | 18 |
| agriculture | 0.0280 | 0 | 0 | — | — | — |
| credit | 0.0137 | 3 | 0 | 0.0% | 0.0% | — |
| crypto | 0.1205 | 35 | 4 | 3.9% | 11.4% | 8 |
| energy | 0.0445 | 18 | 2 | 1.3% | 11.1% | 2 |
| equity | 0.0325 | 6 | 2 | 1.3% | 33.3% | 21 |
| industrial_metals | 0.0295 | 9 | 1 | 1.3% | 11.1% | 21 |
| precious_metals | 0.0324 | 33 | 3 | 3.9% | 9.1% | -7 |
| rates | 0.0086 | 12 | 1 | 1.3% | 8.3% | 20 |

## Diagnostics (§7)

| Quantity | Value |
|---|---|
| Hours without quorum | 1.3% |
| Escalations | 10 |
| Hours suppressed by the cooldown | 30 |
| Early breaks refused by the debounce | 0 |
| basket_coherence fires | 0.3% |
| pca_sync fires | 0.0% |
| Correlation of the two (§3.4, drop one above 0.7) | — |
| csv_compression fires (retired, §21) | 0.0% |
| Calendar multiplier above 1, all hours | 17.3% |
| Calendar multiplier above 1, at events | 59.7% |
| Escalations inside a calendar window | 90.0% |
| Escalations the calendar multiplier decided | 90.0% |

Escalations by reason: `si>=escalation_threshold` 10

Share of events on which each trigger was true: `cluster_shift` 100.0%, `price_shock` 44.2%, `saed_breadth` 18.2%, `single_factor` 9.1%, `sustained` 0.0%, `volume` 18.2%

## Events by month

```
2023-07  # 1
2023-08  ### 3
2023-09  ### 3
2023-10  # 1
2023-12  # 1
2024-01  # 1
2024-02  ## 2
2024-03  ## 2
2024-04  ## 2
2024-05  ### 3
2024-06  ## 2
2024-07  # 1
2024-08  ## 2
2024-09  ## 2
2024-10  # 1
2024-11  # 1
2024-12  ### 3
2025-01  ### 3
2025-02  # 1
2025-03  # 1
2025-04  #### 4
2025-05  ### 3
2025-06  ## 2
2025-07  # 1
2025-08  ## 2
2025-09  ### 3
2025-10  # 1
2025-11  # 1
2025-12  ## 2
2026-01  ## 2
2026-02  # 1
2026-03  #### 4
2026-04  ## 2
2026-05  ### 3
2026-06  ## 2
2026-07  #### 4
2026-08  ## 2
2026-09  ## 2
```
