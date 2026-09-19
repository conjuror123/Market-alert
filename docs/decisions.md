# Why it is built this way

The reasoning behind the choices that are easy to second-guess, so they are not
re-litigated. Each one is settled by measurement; where a number appears, it is the
number that settled it.

---

## The ladder

**A return period, not a score.** "The largest move in about three years" needs no
calibration intuition; a 1-to-100 importance score does. It also solves comparison for
free: 1.5% is unremarkable in SOL and a once-a-year event in SHY, but "once a year" means
the same thing everywhere.

**A record, not a fit.** A rung is literally the biggest move in its own lookback, so
the claim is a fact about the instrument's record rather than an estimate from a tail
model. This is exactly calibrated by construction: for any distribution whatever, the
probability that the newest of N observations is the largest of those N is 1/N, so "the
largest in the trailing six years" happens about once every six years because there is no
model to be wrong. Measured on the archive: 1.21 per six years, 29 records against 23.8
expected — within Poisson noise. Across the four rungs the absolute ladder reads
0.73 / 1.09 / 1.05 / 1.18.

A fitted tail cannot do this at our sample size. The same instrument fitted with the same
code on different six-year windows put SPY's once-in-six-years level anywhere from 2.22%
to 6.78% — a factor of three, decided purely by which six years the window held.

**Fitted per instrument, never pooled.** Pooling puts SHY and SOL back on one yardstick,
which is the thing the ladder exists to avoid.

**An instrument cannot overclaim.** It cannot be "the biggest in six years" until it has
six years, because the answer is a lookback into a record that does not exist yet. No
extrapolation limit is needed; it is the shape of the arithmetic.

**The top rung is six years** because six is inside the five-to-ten the recipient asked
for. It is not tuned to the archive: setting the boundary to fit how much history happens
to be downloaded would mean deepening an instrument quietly renames moves already sent.

**Read the two ladders separately, never their union.** Each makes its own claim and each
is separately calibrated. A message takes whichever rung is rarer, so the *rate* of
messages is roughly the sum of the two — 2.03× at `extreme`. That is a volume figure, and
volume is what `sensitivity` turns. It is not a miscalibrated rung.

**Known limit — when an instrument's shocks happened.** The six-year rung fires about
2.4× too often for the currency pairs and 0.34× for equity and credit. This is not an
estimator fault: it tracks *where in its own life* each instrument's worst moves fell.
Taking the median position of each instrument's twenty largest moves, 0 being the start of
its history and 1 the end:

    FX        0.74   fires 2.43×
    energy    0.70          1.35×
    rates     0.53          1.36×
    equity    0.42          0.34×
    credit    0.37          0.34×

Correlation 0.543. FX's worst hours — the 2015 franc unpeg, Brexit, the 2022 yen — are
all in the last quarter of its history, so every fit before them was set on a market that
had not yet shown what it could do. Equity and credit carry 2008 in their first half. No
fit that refuses to look forward can know which of the two it is in, and refusing to look
forward is worth the cost.

**What a record gives up.** It says a move beat everything in six years, not by how far,
so ordering within a tier needs the magnitude alongside — which is why the message carries
both. And records cluster: a crisis produces several in a week. That is a property of
markets, not an artefact.

---

## The residual

**The market model, with an estimation gap.** `r = alpha + beta·F + e`, a rolling
500-bar regression ending three bars before the bar being judged. Three bars because the
leak the field worries about is short, and three of five hundred does not measurably move
the coefficients.

**The regressor is the instrument's own block factor**, oriented by sign. Orientation
matters: within FX the dollar-quoted and dollar-based pairs otherwise cancel and the
factor reads near zero. A basket-wide factor was tried and dropped — it does not scale to
sixty instruments the way a block factor does.

**Leave-one-out in the BMP denominator**, which the published form does not do. In an
ordinary event study every firm shares one event date and the statistic is about their
average. Here each instrument is tested individually against its peers, so without
leave-one-out a genuine single-asset move inflates the very spread it is measured
against — the instrument raises its own bar and hides itself.

**It is a t, not a z.** Dividing by a *sample* spread over as few as five peers makes a
t. Wallace's normalising transform maps it to z for the degrees of freedom actually
present; a floor on the denominator would not have been correct.

**The rank-test gate.** BMP predicts the cross-sectional spread S ≈ 1, and over all
scored hours its median is 0.963. In 0.9% of hours it falls below 0.3 — the whole block
asleep, every residual an order of magnitude smaller than its own sigma predicted — and
dividing by 0.05 turns a raw z of 0.86 into 4.05. The event-study literature names this
failure: Campbell & Wasley found the standardised test misspecified for thin trading,
because near-zero returns corrupt the variance estimate, and recommend a non-parametric
rank test that estimates no variance at all.

So an hour claimed by the **abnormal channel alone** that Corrado's rank test contradicts
has that claim withdrawn. The "alone" is load-bearing: the rank test is itself
misspecified when variance jumps, which is exactly when the absolute channel fires, so the
gate lifts precisely where the rank test stops being trustworthy. Of 24 pushes in October
2008 it removes one; of 15 in March 2020, none.

**Peer count is not the discriminator.** Fewer than ten peers is 37.5% of all scored
hours — the shape of a 24-hour basket whose equities trade six and a half, not an
anomaly. The thin hours hold the best calls: the SNB unpegging the franc, the yuan
devaluation, Brexit, post-Fukushima, the 2024 yen intervention, all with nine peers or
fewer and all with a spread well above 1. What matters is whether the peers were *moving*.

---

## What counts as an event

**A one-cent move is unobserved, not small.** At a $0.01 tick a 0.025% move in SHY is one
tick of jitter. Anything under two ticks is dropped.

**An event carries one bar's numbers.** When an event escalates inside its day it keeps
the higher tier *and* that bar's move — reporting the opening bar's magnitude beside a
later bar's tier produced pushes reading "biggest move in 3 years, +0.01%".

**The peak moves at the same tier.** `extreme` is the top of the ladder, so a
strictly-higher rule can never fire for it. On 2015-01-15 USD/CHF opened `extreme` at
−3.5% and did −10.5% an hour later; the peak now also moves on a bigger move at the same
tier, ranked by exceedance so the two channels stay comparable.

---

## Who gets interrupted

**Rarity and urgency are different questions.** Rarity is a property of the instrument
and means the same whether five instruments are watched or fifty. How often someone is
willing to be interrupted is a property of the person and does not grow with the
watchlist. Keeping them apart is what stops the alert rate tripling the day three
instruments are added.

**One event, one interruption, one day.** An instrument may open one event per trading
day. A calendar day rather than a rolling window, so the reader can say when the next one
can come; the UTC boundary because 00:00 UTC is about the quietest hour there is, where a
local midnight lands at 21:00 UTC in the middle of the American session.

**Both push tiers go out immediately, and are edited afterwards.** A move given back
within hours is not news, and there is a cheap way to know: wait and look. Of events still
standing at their own day's close, 80% were still standing at the next day's close,
against 26% of those that had already given it back. But a once-in-three-years move that
arrives six hours late is a worse product than one that arrives now — so the message goes
at once and is edited in place at this day's close and the next day's close with how the
move actually held. Retention decides what the message *says*, not whether it is sent.

**The system does not count its own alerts.** There is no weekly cap. A detector that
goes quiet on the third alert of the week answers a question about the reader's patience
with an instrument's price history, and it fails adversarially: the week the franc is
unpegged is exactly the week a budget starts silencing things, because that is the week
records cluster. Volume is controlled where it is generated — by the ladder, by
`sensitivity`, and by the size floor.

**A closed note is a record, not a feed.** Every note still being tracked is re-rendered
from the events table on each run, which is what lets a late event appear and a
recomputed-away one go. Editing is silent in Telegram; posting a part is not. So a note
keeps being corrected for as long as it is tracked, and stops being able to grow a few
hours after its period ends. Without that bound anything that changes the events table
changes closed notes too, and what they gain arrives as new messages: one cold rebuild
grew a note that had closed two days earlier from 5 rows to 19 and posted the difference
as two alerts at breakfast. The rows were right. The interruption was not.

**Say what the move was big compared with.** 45% of pushes carry a number under 1%, and
"+0.13%, biggest move in about three years" reads as a bug. Short Treasuries move 0.024%
in a usual hour, so it really is six times normal. Both numbers are shown, so the claim
is checkable.

---

## How it is scored

**Precision is the wrong yardstick.** Delaying every alert by six hours *raises*
precision, from 52.0% to 56.0%. No predictive measure can behave that way — precision
here credits coincidence as much as prediction.

**Recall on the obvious is the right one.** Of the hours in the top 0.01% of an
instrument's *own* distribution, how many reached the reader? It should be 100%. The
counterpart — how often a below-median hour fires — should be 0%. Neither needs episode
labels or a threshold anyone can argue about. Current figures are in `README.md`.

**Judge each event on the quantity its own ladder scores.** `absolute` against the raw
return, `abnormal` against the standardised residual. Swapping the two yardsticks scores
them at 3.5% and 1.1% and means nothing except that they were swapped.

**Recall is per episode, not per hour.** One shock spans several bars and the detector
reports the peak, so a per-hour figure would mostly measure the debounce.

---

## The data

**The store is unadjusted, with ex-dates flagged.** Adjusted series are recomputed
retroactively on every dividend, so a stored history built from them changes underneath
the record the ladder is made of. Ex-dividend drops land in the gap channel rather than in
`r`.

**Corporate actions are declared, not inferred.** `divCash` and `splitFactor` come from
Tiingo's daily endpoint. Inferring a dividend step from the ratio of an adjusted to an
unadjusted series cannot see a share split at all — both series are split-adjusted, so
the split cancels — and a vendor that divides a nominal pre-split dividend by a
split-adjusted price then reports every earlier step at twice its true size.

Splits are recorded in the table and **excluded** from the un-adjustment used when
deepening history: the store is already split-adjusted, so applying a split factor would
manufacture the error it is meant to remove.

**The newest rows of the metrics store are never trusted.** The run fires five minutes
past the hour and stores a bar for the hour it is standing in — two to thirteen per cent
of that hour's volume, measured on the committed store. The bars heal by themselves, since
the next fetch returns the complete hour and the incoming row wins the merge. The metrics
did not: an extension computed only hours newer than the store's last one, so the complete
bar arrived to find its hour already written and was never scored. Every hour was judged
on its first five minutes, which understates every move and misses precisely the
news-driven hours the system exists to catch — 2026-09-16 18:00, the FOMC statement, went
into the store as SHY +0.02% when the hour had closed at -0.19%, and thirteen instruments'
events went with it. `extend_asset_metrics` now re-scores its last two days of bars rather
than trusting them, which costs nothing: the chain already recomputes `warm_bars` of
lead-in to be exact, and this keeps more of what it computed.

**A day is not a unit of completeness.** Gap detection asks about hours, not days: a day
present with three of its seven hours is a hole the calendar can see and a day-level check
cannot.

**One provider per instrument, chosen by measurement.** Each candidate was compared
against the stored bars hour by hour, in basis points, with one sigma of an hourly move
(20–40 bps for these instruments) as the yardstick. A feed that disagrees by a few basis
points on a thin fund is not a cheaper feed — it is a source of alerts for moves that did
not happen. `docs/architecture.md` has the resulting split.

---

## The shape of the repository

**Derived data is not tracked.** Metrics, residuals and the event table are several
hundred megabytes rewritten in full on every run, and they rebuild from the bars in about
two minutes — which the hourly job does anyway.

**Bars and the tables that delta well commit once a day, not hourly.** Appending to
Parquet leaves the earlier row groups byte-identical, so a day of new bars across all
files costs about 1 MB. Hourly commits would be twenty-four times that for the same
information.

**Two stores, not one.** Parquet for the columnar history, JSON for state where a
whole-file rewrite is the point. Nothing here needs a server.
