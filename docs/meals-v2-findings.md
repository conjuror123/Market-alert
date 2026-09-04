# MEALS v2 — what the rebuild found

v1 was cleared away and the detector rebuilt on what the literature recommends: a
HAR volatility forecast in place of a sum of binary triggers, the strong signals as
levels rather than as conditions, and a threshold that was meant to follow the regime.

Two results, and the second one matters more than the first.

## 1. The volatility forecast works

Walk-forward, refit weekly, fitted only on rows whose target had already resolved:

| model | R² | correlation |
|---|---:|---:|
| trailing 24h volatility alone | 0.099 | 0.377 |
| HAR ladder, four horizons | 0.158 | 0.425 |
| HAR + downside volatility | 0.163 | 0.427 |
| all levels | 0.165 | 0.426 |
| standardised features only | **−0.114** | 0.172 |
| **levels + standardised together** | **0.200** | **0.456** |

Two things worth keeping from that table. The multi-horizon ladder beats the single
horizon by 60% in R², which is the HAR claim reproduced on our own data. And
standardising the features *alone* is far worse than the naive benchmark — it removes
the level of recent volatility, which is most of what predicts forward volatility. The
regime-relative view is a useful addition and a disastrous replacement.

Fitting on log volatility rather than raw was also necessary: on the raw scale both the
model and the benchmark scored a negative R², because least squares chases the tail of a
right-skewed quantity and fits the body badly.

## 2. The thing §7 asks about is close to unpredictable

Same features, same machinery, two different targets:

| target | R² | correlation |
|---|---:|---:|
| basket volatility over the next 24h | **0.200** | 0.456 |
| max block move against its own Q99, next 24h | **0.020** | 0.247 |

That is the finding. **Volatility is forecastable; "will some block exceed its 99th
percentile 24-hour move" is very nearly not.** An R² of 0.02 is not a model that needs
better features — it is a question with little forecastable structure at this horizon.

It also explains everything that came before it. v1's F1 of 23% against a trailing-SPY
rule's 25%; the collapse from 28% to 9% out of sample; the two matched-horizon channels
that earned weight and changed nothing. None of those were implementation failures. They
were a detector being asked a question whose answer is mostly noise.

The threshold experiments say the same thing from another side. A rolling quantile fires
a constant fraction of hours by construction, so it cannot fire less when the market is
calm — it went the wrong way, 1.44 against a target of 0.41. A fixed cut on the forecast
fires nothing at all in the calmer period. Neither tracks the episode rate, because the
forecast's own level barely moves between the periods (−6.172 against −6.182) while the
episode rate falls by more than half. The forecast is not blind — it is answering a
different question from the one the label asks.

## 3. What follows

The useful product is not the one the specification describes. We can say, with real
skill, **"the next day is likely to be turbulent"**. We cannot say **"something
extraordinary is about to happen to one of these blocks"**, and four phases of work now
point at that being a property of the market rather than of our code.

A volatility-regime alert is a smaller claim and an honest one: R² 0.20 out of sample,
against a benchmark of 0.10, from a linear model with a dozen interpretable
coefficients. It would fire when conditions are turning, not when a threshold is
crossed, and it would be right about as often as the physics allows.

What is built and working: `meals/features.py` (HAR ladder, downside volatility, the
strong signals as levels, everything also standardised), `meals/forecast.py` (walk-forward
ridge HAR with an honest resolved-target rule), `meals/threshold.py` (adaptive and fixed
cuts, plus the labels-free firing-rate check), `meals/detector.py` (the chain end to end).

The data layer, the reference calendar, the quality gate, the per-asset metrics and the
basket aggregates were kept untouched throughout and did not need changing.
