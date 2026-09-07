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
(`tremor.market`), not whether its pass rate is flat - which the original plan could not
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

---

# Step 2 done — Corrado ranks, and why they are recorded but not routed on

`residuals.rank_statistic` adds `t_rank`, `rank_pct` and `rank_confirms`; every event
carries `rank_confirms` as a field of its own, beside `basis` rather than inside it —
`basis` says which channel CLAIMED the event, this says whether a test sharing none of
their assumptions concurs.

**Two departures from the published form, both forced by what it is being used for.**

*It ranks the magnitude, not the signed abnormal return.* The two channels it must agree
or disagree with are both two-sided magnitude tests, and agreement is only meaningful
between tests asking the same question.

*It confirms rather than grades.* Corrado's statistic is a rescaled rank, so it
saturates: over a five-hundred-bar window the most extreme bar scores 1.729 and the fifth
most extreme 1.701. It can say "the most extreme hour in five hundred" and never how much
more extreme, so it cannot feed a return-period ladder. Confirmation is set at the top 1%
of the window — not a taste threshold: the mildest tier is a once-a-fortnight event,
which at this basket's bar rates is between one bar in 98 and one in 336, so a test
confirming more freely than the ladder's own floor would agree with everything.

**It is genuinely informative about reversal.** Over all 13,591 events:

| | events | held (≥0.5) | reversed (<0) |
|---|---:|---:|---:|
| rank confirms | 5,172 | 66.9% | **19.6%** |
| rank disagrees | 8,061 | 56.8% | **34.4%** |

And its effect is largest exactly where it should be — on the abnormal channel, the
parametric test most exposed to fat tails and the one that scored worst on the yardstick.
Among its pushes: 10.4% reversed when the rank test agrees, 25.5% when it does not.
Three-way agreement (both channels and the rank test) gives 173 pushes at 80.3% held and
10.4% reversed; a single channel with the rank test dissenting gives 119 at 70.6% and
25.2%.

**And it is uninformative about the §7 label, which is why routing does not use it.**

| | alerts | Precision | Recall | F1 | reversed |
|---|---:|---:|---:|---:|---:|
| pushes as they stand | 106 | 19.8% | 21.5% | 20.6% | 14.4% |
| require rank agreement | 91 | **19.8%** | 16.2% | 17.8% | 11.2% |
| the ones it would drop | 15 | **20.0%** | 5.4% | 8.5% | 34.4% |

Precision is identical to a tenth of a point on all three rows. The events the rank test
rejects are exactly as likely to precede a significant move as the ones it accepts — they
are only more likely to give the move back afterwards.

That is not a contradiction, it is two different questions. Retention asks whether *this
instrument's* move persisted; the §7 label asks whether *the block* had a large 24-hour
move. A rank test on an instrument's own residual history predicts the first and has
nothing to say about the second. **Gating pushes on it would trade a quarter of the
recall the yardstick measures for an improvement the yardstick cannot see**, on the
strength of one measure out of two.

So it is recorded, exported and available — a reader wanting to know how likely an alert
is to be given back has it — and the routing is unchanged until there is a reason
visible in more than one measure.

**The rule this is an instance of.** Two quality measures that disagree are more useful
than one that agrees with you. The retention table alone would have justified this change
comfortably.

---

# Step 5 done — the OU residual is computed, the reversion filter is not used

`residuals.ou_fit` fits Avellaneda and Lee's construction: the cumulative residual as an
Ornstein-Uhlenbeck process, giving a reversion speed, an equilibrium, and the s-score.
Every event carries `ou_reverts`. The rolling fit runs on the global cumulative sum
because the AR(1) slope — and with it the reversion speed and the s-score — is invariant
to where each window's sum starts, which a test asserts.

It works, in the sense that it makes the distinction it is supposed to make. On synthetic
series, pure noise reverts in 86 bars and is accepted 74% of the time; a drift reverts in
1,687 bars, is accepted 0% of the time, and produces a median |s| of 21 — the trap the
speed check exists to catch, since on the s-score alone a drift looks extraordinary.

**On our data it separates nothing.**

| | events | held (≥0.5) | reversed (<0) |
|---|---:|---:|---:|
| reverts fast (A&L trust) | 10,662 | 60.6% | 28.7% |
| drifts (A&L refuse) | 2,571 | 61.4% | 28.3% |

And against the §7 yardstick it runs backwards: pushes whose residual drifts score 25.0%
precision, those that revert 18.9%. The s-score adds nothing either — its correlation
with retention is +0.017, against |z_resid|'s +0.023, and it is not monotonic across
quartiles.

**There is a reason, and it is not a defect in the fit.** Avellaneda and Lee are
statistical-arbitrage traders. They need the residual to come back, because coming back
is how the position closes at a profit; a residual that keeps going is the one that
bankrupts them. **An alerting system wants the opposite.** A move that matters is
frequently a move that persists — this system's own routing will not push a once-a-year
event unless it held — so their filter rejects, by construction, a large part of what we
are trying to send.

The plan anticipated the question and refused to settle it by analogy: *"they want
reversion because they trade it, and a move that matters may well be a move that keeps
going. The flag is computed; what uses it is decided on measurement."* It was, and the
measurement says do not use it.

The fit is kept: it is cheap, it is recorded and exported, and `ou_reverts` is the honest
answer to "is this an unmodelled factor" for anyone who wants it. Nothing routes on it.

---

# Where the six steps ended up

| step | outcome |
|---|---|
| 1 BMP standardisation | **adopted.** Breaches in the widest fifth 97.1% → 54.1%; the abnormal channel went from concentrated to flat (widest/calmest 3.7×) |
| 2 Corrado ranks | implemented, recorded, **not routed on** — predicts reversal, not the label |
| 3 Patell inflation | implemented, **negligible at L=500** — the correction is k²/(L−1) |
| 4 estimation gap | **adopted.** Three bars, and it caught an off-by-one in step 5 |
| 5 OU + reversion filter | implemented, **not used** — built for traders who need reversion |
| 6 second factor | not started |

One of five borrowings transferred outright. That is not a criticism of the plan, which
was written from the literature before any of it had been measured here and which said at
the outset that steps 1 and 2 had an acceptance test needing no labels. It is the reason
the acceptance test was worth insisting on: a method's standing in its own field is
evidence about its own field, and the only evidence about this one is a measurement on
this data.

**What is actually left.** Step 6, and it now has a sharper question than "does the
basket show common structure": the abnormal channel scores 5.4% precision against the
yardstick while the absolute channel scores 44.4%, and the abnormal channel is the one a
second factor would change. If a second component removes structure the first misses, it
should show up there and nowhere else.

---

# Step 6 done — not a second factor: the block factor was cancelling itself

The step asked whether the residuals still carry common structure, and to test a second
principal component if so. They do — seven components above the Marchenko-Pastur noise
ceiling — but the second one turned out not to need a new factor.

**What the residual components are.** PCA on the residual panel (19 instruments, 33,109
hours where 80% of the basket is present):

```
PC1  15.0%   GLD -0.44  SLV -0.39  IEF -0.32  USO +0.32  USD/JPY +0.32   (real assets vs the dollar)
PC2  12.2%   AUD -0.53  EUR -0.51  GBP -0.50  NZD -0.26  JPY -0.22       (75% FX)
PC3  10.1%   QQQ +0.49  SPY +0.40  IWM -0.36  XLF -0.29                  (within equities)
```

PC2 is the dollar, and the FX block factor is supposed to have removed it.

**Why it did not.** The block factor is the median of the other members' returns, and a
median represents a common move only if the members respond to it with the same sign. The
FX block holds three pairs with the dollar as quote (EUR/USD, GBP/USD, AUD/USD) and three
with it as base (USD/JPY, USD/CHF, USD/CAD). A dollar rally sends half down and half up,
and **the median of three negatives and three positives is nearly zero.** Measured over
142,400 hours with all six present, on the 1% of hours the dollar moves most:

```
the actual dollar move (sign-corrected)   40.72 bp
what the block factor sees (plain median) 10.82 bp
```

Three quarters of it was leaking into all six residuals at once — which is what PC2 was.

**The fix.** `Asset.block_sign` orients each member from its ticker before the median is
taken, and flips the result back so `beta_block` keeps its meaning. Taken from the ticker
rather than fitted, because it is a fact about how the pair is quoted and a sign estimated
per window could flip between windows, which is worse than not correcting.

It does what it was meant to: PC2's eigenvalue falls 2.31 → 1.99 and its FX share 75% →
35%. PC1 is untouched at 2.84 → 2.91, correctly — a gold-versus-oil factor crosses blocks
and no block factor can absorb it.

**And it makes the detector slightly worse on both quality measures.**

| | before | after |
|---|---|---|
| yardstick, all pushes | P 19.8% R 21.5% **F1 20.6** | P 16.8% R 19.2% **F1 17.9** |
| retention, all pushes | held 75.8%, reversed 14.4% | held 73.7%, reversed 15.5% |
| absolute channel precision | 44.4% | **50.0%** |
| absolute channel regime concentration | 81.3× | **61.3×** |

The likely reason is the one the earlier steps kept running into: removing more common
structure moves the abnormal channel from "big move" toward "genuinely unexplained move",
and both available measures prefer big moves — the §7 label by construction, and retention
because a market-wide move persists more reliably than an idiosyncratic one. A dollar
rally leaking into six residuals made six pairs fire, and a dollar rally is a macro event
worth hearing about.

**Kept anyway, and this is a judgement call where the numbers mildly disagree.** The
difference is three episodes out of 130 and nineteen extra alerts, which is not much above
noise for this sample; the modelling defect is definite and measured at four-to-one. A
factor that silently cancels itself is also a latent hazard for anyone who later trusts
`beta_block` to mean what it says. Reverting is one commit if the measured numbers are
preferred to the cleaner model.

**One thing checked and found already true.** The block factor's own stated purpose is
stopping single-asset detection from firing in blocks. FX abnormal events never fired
three-at-once either before or after (0.0%, max 2 both ways) — that part was already
working, and the leak was showing up in the residuals rather than in simultaneous events.
