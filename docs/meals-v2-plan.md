# MEALS v2 — a plan

MEALS v1 is finished, calibrated, and tested twice. It reaches F1 23.0% against a
trailing-SPY rule's 25.0%, and its test-period F1 sits at ~9% on two independent
splits. The system works; the design does not generalise across volatility regimes.
This is the plan for what to do about it.

Written after reading what the field already knows, because most of what took us four
phases to measure is documented.

---

## 1. What the literature says we got wrong

**Breadth and correlation are the weak signals.** A 2026 study built almost exactly
this system — PCA on a cross-section of SPY plus eleven sector ETFs, residual stress
from reconstruction error — and concluded that average pairwise correlation and breadth
are *weak standalone warning scores*, while volatility, VIX, downside semivolatility and
cross-sectional dispersion perform more strongly. Residual stress has value as a
*conditional complement*, not as a primary detector.

MEALS makes breadth (cluster shift) and single-factorness (PCA, correlation) its two
headline triggers. It computes CSV and VIX — both named strong — and uses CSV only
inside a compression condition that fires zero times, and VIX only as a 1.3x multiplier
active in 3% of hours. We built the strong signals and discarded them.

**Returns are near-unpredictable; volatility is predictable.** We built a return-structure
detector to predict what is essentially a volatility event. The predictable half of the
problem is the half we did not model.

**A pure return-magnitude label fights the data.** Extreme events concentrate in
persistent high-variance regimes; a label defined on return magnitude alone "treats
extreme events as isolated observations". Replacing it with a volatility-aware label
took PR-AUC from 0.06 to 0.40 in the published work. §7's label is a pure
return-magnitude label.

**Thresholds should be time-varying.** Realized peaks-over-threshold fits the tail to
conditional volatility; the streaming variant produces an adaptive decision threshold in
non-stationary streams. This is the direct cure for a firing rate that stayed at 0.98x
while the episode rate fell to 0.41x.

**Multi-horizon is the mechanism that works.** HAR combines daily, weekly and monthly
components because volatility has long memory no single horizon captures. Our
`trigger_sustained` was a one-horizon rediscovery of a three-horizon idea.

Sources are listed at the end.

---

## 2. What we keep

Most of it. The failure is in the score, not the plumbing.

| keep as-is | why |
|---|---|
| `bars`, `sessions`, `quality`, `returns`, `basket` | the data layer is sound and expensive to rebuild |
| `zscore`, `volume`, `residuals` | correct implementations, still needed as features |
| `versioning`, `journal`, `export` | reproducibility machinery, byte-identical reruns, verified |
| `truth`, `evaluate` | the yardstick and the harness, including episode scoring |
| `calibrate` | the search, the fold objective, the freeze discipline |
| `cluster` | the cooldown automaton turns a score into alerts — feed it a different score |
| `calendar_multiplier`, `vix` | tiered calendar and VIX windows, both measured |

**What changes is `si_index`** — the score itself — plus new feature and model modules.
`si_index`'s trigger sum is not deleted; it becomes one complement among several.

---

## 3. Phases

### Ф8 — Replace the score with a volatility forecast

**Ф8.1 `meals/features.py`.** A feature matrix on the reference-hour grid:
- HAR components: realized volatility of the basket over 1h, 24h, 120h (a trading
  week) and 480h (a month), all strictly trailing;
- the strong signals as levels, not conditions: `csv_norm`, VIX, downside
  semivolatility (realized volatility of negative returns only);
- the complements, kept because the residual-stress paper says that is what they are
  good for: breadth share, `pc1_ratio`, `mean_pairwise_corr`, the SAED count, the
  calendar multiplier;
- everything scaled by its own trailing distribution so a feature means the same thing
  in 2022 as in 2025.

**Ф8.2 `meals/forecast.py`.** Predict realized basket volatility over the next 24
reference hours. Start with a linear HAR regression — interpretable, few parameters,
the field's own baseline — and only add complexity if the linear model is beaten by
something we can still explain. Estimated strictly on data through t-1.

**Ф8.3 `meals/threshold.py`.** Turn the forecast into alerts with a time-varying
threshold: a rolling quantile of the forecast's own recent distribution, with a
peaks-over-threshold tail fit where the quantile is unstable. Success here is
mechanical and checkable without labels: **the firing rate must track the episode
rate**. Today that ratio is 0.98 against 0.41.

**Ф8.4 A volatility-aware label**, beside §7's strict one and the 0.75 near label. An
hour is significant if a large move follows *or* realized volatility enters its upper
tail. All three reported; the strict one stays the headline for continuity with v1.

**Ф8.5 PR-AUC as the primary metric.** Threshold-free, so it stops conflating "is the
score informative" with "is the threshold well chosen" — the two questions v1 kept
mixing. Precision and recall still reported at the operating point.

### Ф9 — Validate on a fresh cross-section (available now)

Calibrate on the current 21 instruments; test on a disjoint universe — European and
Asian indices, other sectors, other crypto. This tests generalisation across
cross-sections rather than across time, and it is the only genuinely unlooked-at
evidence we can get without waiting. Needs a second basket configuration and its bars;
the rest of the pipeline is basket-agnostic already.

### Ф10 — Validate out of time (needs waiting)

The data from 2026-09 onward is untouched by any decision made so far. First read at
roughly four months, a solid read at a year.

### Ф11 — Ship, or stop

If it passes, wire to Telegram alongside the existing per-asset alerts. If it does not,
that is a result too, and the honest move is the simpler detector.

---

## 4. Pre-registered success criteria

Written before the test is run, so the answer cannot be reinterpreted afterwards. v2 is
worth shipping if, on a test period not used for any decision:

1. **PR-AUC beats the trailing-SPY baseline** on the same labels and the same window.
2. **The firing rate tracks the episode rate** — ratio within roughly 0.7-1.4 of it,
   against v1's 2.4x mismatch.
3. **Precision at the operating point is at least 35%** under the strict §7 label at no
   fewer than 0.3 alerts a week — i.e. it beats the trailing-SPY rule's 36.9% while
   staying useful.

Failing 2 means the regime problem is unsolved regardless of the headline F1.

---

## 5. Discipline

Every number produced against a spent period is labelled a **development** result, never
evidence of generalisation. One test per frozen `config_version`, as §7 requires. The
freeze record goes in `data/meals/frozen.json` before the test is read, as it did for
both v1 tests.

And the honest caveat that applies to all of it: the redesign in Ф8.3 is motivated by an
observation made on a test period. That makes the old data unusable for judging it and
makes Ф9 and Ф10 the only real evidence.

---

## Sources

- [Volatility-Aware Extreme Event Detection in High-Frequency Financial Markets](https://arxiv.org/html/2607.17555v1)
- [Beyond Volatility: A Leakage-Safe Residual-Stress Signal for Drawdown Risk Monitoring](https://www.mdpi.com/2227-9091/14/7/143)
- [The usefulness of cross-sectional dispersion for forecasting aggregate stock price volatility](https://www.sciencedirect.com/science/article/abs/pii/S0927539816300020)
- [Cross-sectional return dispersion and stock market volatility: evidence from high-frequency data](https://onlinelibrary.wiley.com/doi/10.1002/for.2959?af=R)
- [Realized Peaks over Threshold: A Time-Varying Extreme Value Approach](https://academic.oup.com/jfec/article/17/2/254/5369812)
- [Forecasting realized volatility in the stock market: a path-dependent perspective](https://arxiv.org/html/2503.00851v2)
- [Dynamic Calibration of Decision Thresholds for Financial Anomaly Detection](https://www.sciencedirect.com/org/science/article/pii/S1062737525001118)
