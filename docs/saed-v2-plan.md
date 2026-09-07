# SAED v2 — what the event-study literature already solved

The single-asset detector is the product: per-asset alerts, extensible to any new
instrument. This is what fifty years of event-study methodology says we should change,
and one measurement showing the biggest problem is real and present in our data.

## The thing SAED already gets right

`r_i,t = alpha_i + beta_i * F_t + e_i,t`, estimated out-of-sample on a rolling window,
is the **market model** — the benchmark used in the large majority of published
short-horizon event studies. The abnormal return is the residual. We are on the
standard path, not off it. What is missing is everything the field added afterwards to
stop that residual lying to you.

## The measurement that matters

Bucketing every hour by the cross-sectional spread of Z across the basket:

| cross-section | hours | breaches per hour | share of all breaches |
|---|---:|---:|---:|
| calmest fifth | 6,872 | 0.000 | 0.0% |
| second | 6,871 | 0.000 | 0.0% |
| third | 6,871 | 0.001 | 0.1% |
| fourth | 6,871 | 0.020 | 2.8% |
| **widest fifth** | 6,872 | **0.674** | **97.1%** |

**97.1% of single-asset breaches occur when the entire cross-section is wide.** The
calmest 40% of hours produce none at all. The detector is not finding idiosyncratic
moves; it is finding market-wide volatility and naming whichever asset moved most. The
60% overlap between SAED events and active cluster events says the same thing from the
other side.

This is textbook **event-induced variance**, and the fix has a name.

## Step 1 — BMP standardisation (the big one)

Boehmer, Musumeci and Poulsen: standardise the abnormal return as usual, then **divide
again by the cross-sectional standard deviation of those standardised returns in the
same event window**. The denominator widens automatically when the event makes
everything volatile, so a move only counts as unusual if it is unusual *relative to what
every other asset is doing right now* — which is what "idiosyncratic" was supposed to
mean all along.

It is the modern default short-window parametric test precisely because it stays valid
when events raise volatility, without giving up much power. Concretely: divide
`z_resid_i,t` by `std_j(z_resid_j,t)` over the assets in session that hour, then apply
the threshold to that.

## Step 2 — Patell's prediction-error inflation

Our residual is an **out-of-sample forecast error** from the rolling regression, so its
variance is strictly larger than the in-sample residual variance we divide by. Patell
inflates it by three terms: the base residual variance, the estimation error in the
fitted mean, and a **forecast-extrapolation penalty** growing with how far the current
factor value sits from its estimation-window average.

That third term matters here more than anywhere: on a big market day `F_t` is far from
its mean, the beta estimate extrapolates, the residual is genuinely noisier — and we
currently treat it as if it were not. It is the same bias Step 1 attacks, from the
model side rather than the cross-section side.

## Step 3 — A real estimation gap

Standard practice leaves the estimation window **disjoint** from the event window, with
a gap, so that a move leaking in before the event does not contaminate the estimate of
normal. We shift the coefficients by exactly one bar. A gap of a few bars costs almost
nothing and closes a leak the field considers basic hygiene.

## Step 4 — Model the residual as Ornstein-Uhlenbeck, not as noise

Avellaneda and Lee model residual returns as **mean-reverting OU processes** and trade
the **s-score**: the distance from the residual's equilibrium in units of its
equilibrium standard deviation. Two things follow that we do not have:

- the equilibrium mean and variance come from a fitted OU process, not an EWMA that the
  event itself contaminates;
- the fit yields a **mean-reversion speed**, and they only trust a signal when
  reversion is fast enough. A residual that drifts instead of reverting is not
  idiosyncratic noise — it is an unmodelled factor, and firing on it is a mistake our
  system currently cannot detect.

## Step 5 — A non-parametric second opinion

Returns are fat-tailed; a parametric threshold on them is optimistic. The Corrado rank
test converts abnormal returns to ranks within the pooled estimation and event window,
assumes no normality, and is immune to outliers — excellent for exactly our case, a
single-hour window.

The field's own house rule is to run **one parametric test and one non-parametric test
and treat agreement as the evidence**. That maps onto machinery we already have: §3.1's
hybrid condition is already an AND of two legs. Replace them with BMP and rank.

## Step 6 — More factors, if the basket can carry them

Avellaneda and Lee use roughly fifteen PCA components. We use one basket factor plus one
block factor. With twenty instruments we cannot fit fifteen, but it argues for keeping
the block factor rather than treating it as the deviation it is currently recorded as,
and for testing a second principal component before adding anything else.

## Order of work

1. **BMP cross-sectional standardisation** — the measured problem, the standard fix.
2. **Corrado rank leg** — cheap, and it gives the parametric/non-parametric pair.
3. **Patell inflation** — more involved; needs the estimation-window moments carried
   forward from the regression.
4. **Estimation gap** — a one-line change, do it alongside 3.
5. **OU residual and the reversion-speed filter** — the largest change, worth doing only
   once 1-4 are measured.
6. **Second factor** — last, and only if the residuals still show common structure.

Steps 1 and 2 alone should be checkable against the table above: after them, breaches
should stop concentrating 97% in the widest quintile. That is the acceptance test, it
needs no labels, and it can be run before anything else is touched.

## Sources

- [Event Study Significance Tests: Patell Z & BMP](https://www.eventstudytools.com/significance-tests)
- [Expected Return Models for Event Studies](https://www.eventstudytools.com/expected-return-models)
- [Event Study Methodology: A Step-by-Step Guide](https://www.eventstudytools.com/introduction-event-study-methodology)
- [Avellaneda & Lee, Statistical Arbitrage in the U.S. Equities Market](https://math.nyu.edu/inmemoriam/avellaneda//StatArb13030.pdf)
- [arbitragelab: the PCA approach to statistical arbitrage](https://hudson-and-thames-arbitragelab.readthedocs-hosted.com/en/latest/other_approaches/pca_approach.html)
- [sipemu/eventstudy — 11 test statistics including Patell and BMP](https://github.com/sipemu/eventstudy)

---

# Step 1 done — result

`residuals.cross_sectional_scale` and `standardise_cross_section` add `z_resid_bmp`:
each asset's residual Z divided by the leave-one-out standard deviation of its peers'
Z in that same hour. `saed.triggers` takes its relative leg from it, and the adaptive
Q95/Q99 are recomputed on the new series — a standardised score compared against an
unstandardised yardstick would have been worse than doing nothing.

Leave-one-out is a deliberate departure from the classic formulation, where all firms
share one event date and self-inclusion is harmless. Here each asset is tested against
its peers, so including it in its own denominator would let a real single-asset move
inflate the very spread it is judged by — raising its own bar and hiding itself. The
arithmetic is verified cell by cell against a direct computation.

**The leg it was applied to is fixed.** Pass rate of each leg by how wide the
cross-section is:

| cross-section | relative leg | absolute leg |
|---|---:|---:|
| calmest fifth | 0.75% | 0.04% |
| second | 0.78% | 0.17% |
| third | 0.88% | 0.45% |
| fourth | 1.01% | 1.23% |
| widest fifth | 1.74% | 5.99% |
| **widest / calmest** | **2.3x** | **139.9x** |

The relative leg is now nearly flat across regimes. Every bit of the remaining
concentration sits in the absolute leg, `|e_resid| >= 3.0 * sigma_LT_resid`, which was
not part of this step and is a raw-magnitude test against a slow-moving long-term sigma:
when the market is loud, every asset's residual is large against its own long-run
average, so the leg passes for everyone at once.

Event totals moved from 2,478 to 1,210, and the share of events in the widest fifth
from 97.1% to 80.0% — but that pair is not a clean before-and-after. The earlier figure
was measured on price Z over the shallower store, and the crypto history has since been
deepened to 2015-2016, so the hour population differs. The leg table above is the honest
evidence: both legs measured on the same data at the same time.

**What this makes the next step.** Not Corrado ranks, as originally ordered — the
absolute leg. Its purpose is a floor, stopping a tiny move from counting merely because
an asset is usually quiet, and that purpose is sound. Its implementation is not: a
long-term sigma makes the floor far too low in a volatile regime. The obvious repair is
to scale it the same way the relative leg is now scaled, which would make the whole
condition regime-relative and consistent. That is a change to what the leg MEANS, so it
is worth deciding deliberately rather than slipping in behind this one.

---

# Steps 3 and 4 done — and step 3 does almost nothing, for a stateable reason

`windows.REGRESSION_GAP_BARS = 3` and `residuals.patell_scale`. Both regressions carry
their estimation-window moments forward with the coefficients, and `score_residuals`
standardises Patell's way: the forecast error divided by the scale a forecast error
actually has, not by the scale an in-sample residual would have had.

**Step 4, the estimation gap, is now a real gap.** The old `shift(1)` was causality — the
estimate at `t` may not have seen `t` — and nothing more. Three bars keeps a move that
begins to leak in before the scored hour out of the estimate of normal. A test asserts
the concrete leak is closed: a single enormous bar cannot move the coefficient used to
judge it or the two bars before it, and does enter from the first bar past the gap.

**Step 3, Patell's inflation, is correct and negligible here.** The plan said "that third
term matters here more than anywhere". Measured, it does not, and the arithmetic says why
in one line. The leverage term is

```
(F_t - Fbar)^2 / ((L-1) * var_F)   =   k^2 / (L - 1)
```

for a factor sitting `k` standard deviations from its estimation-window mean. With
`L = 500`:

| factor at | inflation | effect on the score |
|---|---|---|
| 3 sd | 1.010 | −1.0% |
| 5 sd | 1.026 | −2.5% |
| 12 sd | 1.136 | −12.0% |

Measured over 1,832,114 bars: median 1.0015, p99 1.0257, and only **0.33%** of bars
exceed 1.05. The acceptance table does not move — widest/calmest goes 3.7× → 3.7× on the
abnormal channel, 81.4× → 81.3× on the absolute one, events in the widest fifth 54.1% →
54.0%.

This is not a failed implementation, it is a correct one meeting a window it was not
designed for. Patell's correction is large in the setting the literature uses it in — an
estimation window of about 120 daily observations and an event day whose factor value is
extreme. `L − 1` in the denominator is doing all the work: at 500 bars the estimation
error in the coefficients is small by construction, so the forecast error is barely wider
than the in-sample residual. It is kept because it is right and costs nothing, and
because it becomes the correct behaviour the moment the window is shortened.

**What this makes the next step.** The acceptance table now says the same thing twice:
after BMP the abnormal channel is nearly flat across regimes (3.7×) and **all of the
remaining concentration is the absolute channel (81.3×)**. But the absolute channel is no
longer what §"Step 1 done" described - it is not `|e_resid| >= 3.0 * sigma_LT` any more,
it is the raw return ranked against its own return-period ladder, and its concentration
may be the channel working rather than failing: a market-wide crash is exactly when
residuals are small and raw returns are large, and finding those was why the channel was
added. Deciding that requires asking whether its events duplicate the market-wide channel
(`meals.market`), not whether its pass rate is flat - which the original plan could not
have known, because neither the ladder nor the market channel existed when it was written.

---

# The absolute channel: do NOT flatten it

"Step 1 done" ended by naming the absolute leg as the next thing to repair, on the
grounds that all the remaining regime concentration sits in it (81.3× between the widest
and calmest cross-section, against the abnormal channel's 3.7×), and proposed scaling it
the same way BMP scales the relative leg so the whole condition becomes regime-relative.

**Measured, that would remove the best channel in the system.** Scored against the §7
yardstick, pushes split by which channel fired:

| channel | alerts | caught | Precision | Recall | F1 | lead |
|---|---:|---:|---:|---:|---:|---:|
| abnormal only | 37 | 4 | **5.4%** | 3.1% | 3.9% | 7 h |
| absolute only | 18 | 12 | **44.4%** | 9.2% | 15.3% | 16 h |
| both | 51 | 14 | 21.6% | 10.8% | 14.4% | 14 h |
| all pushes | 106 | 28 | 19.8% | 21.5% | 20.6% | 10 h |

Eight times the precision of the channel BMP was built to fix. Its concentration is not a
defect, it is its function: a market-wide crash is exactly when residuals are small and
raw returns are large, and finding those is why the channel exists. Flattening it across
regimes would make it stop firing when the market moves, which is the only time it has
anything to say.

**That comparison is not neutral, and the reason matters.** §7 calls an hour significant
when a block's 24-hour move passes its Q99 — close to a direct measurement of what the
absolute channel reports, and a question the abnormal channel is not trying to answer. So
the table above is evidence that the absolute channel should stay, and weak evidence
about the abnormal one.

**A label-free test that suits both.** Noise reverts; information is still there a day
later. Retention at 24 hours, on pushes:

| basis | pushes | median retention | held (≥0.5) | reversed (<0) |
|---|---:|---:|---:|---:|
| abnormal | 229 | 1.23 | 73.4% | 16.6% |
| absolute | 119 | 0.84 | 73.9% | 16.0% |
| **both** | 180 | 1.15 | **80.0%** | **10.6%** |

Neither channel is better than the other. **Their agreement is better than either**, and
by the largest margin available anywhere in these measurements: reversals fall by a
third, and `both` reaches the `extreme` tier on 10.2% of its events against 1.3% for
abnormal and 3.2% for absolute.

**Which is the field's own house rule, arriving from the data instead of the reading.**
Step 5 quotes it: *run one parametric test and one non-parametric test and treat
agreement as the evidence*. The two channels here are not that pair — they are two
parametric tests of different questions — but the principle held anyway, and it says what
the next step is worth doing for.

**Revised order from here.**

1. **Nothing to the absolute channel.** It is the most precise thing in the system and
   its regime concentration is what makes it so.
2. **Corrado ranks (original step 2), as a THIRD opinion rather than a replacement leg.**
   The evidence above says agreement is what carries quality, so the value of a
   non-parametric test is that it can agree or disagree with two parametric ones — not
   that it would replace either. Returns are fat-tailed and both current channels are
   parametric, so a rank test is the missing kind.
3. **OU residual and the reversion filter (original step 5).** The retention table is a
   crude version of what the reversion-speed filter would do properly, and the fact that
   the crude version already separates good events from bad is the argument for building
   the real one.
4. **Second factor (original step 6), unchanged and last.**
