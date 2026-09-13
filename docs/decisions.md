# Why it is built this way

Every entry here is a decision that cost something to learn, with the measurement that
settled it. Grouped by what they are about rather than by when they happened — the
chronological record, all 44 sections of it, is in git history at
`docs/tremor-deviations.md` before September 2026.

The rule the rest are instances of: **measure before deciding, and measure the thing you
are about to claim.**

---

## The ladder

**A return period, not a score.** "The largest move in about three years" needs no
calibration intuition; a 1-to-100 importance score does, and nobody ever builds one. It
also solves comparison for free: 1.5% is unremarkable in SOL and a once-a-year event in
SHY, but "once a year" means the same thing everywhere.

**Fitted per instrument, never pooled.** Pooling would put SHY and SOL back on one
yardstick, which is the thing the ladder exists to avoid.

**A RECORD, NOT A FIT — and this replaced peaks-over-threshold.** A rung is now the
biggest move in its own lookback: "the biggest since 3 March 2020" is a fact about the
instrument's record rather than a claim about a distribution. The tiers used to be return
levels fitted by POT — a Generalised Pareto tail above a high threshold, probability-
weighted moments, the whole apparatus from Coles (2001) and Hosking & Wallis (1987) —
correctly implemented and asked a question it cannot answer at this sample size.

**Why it had to go, measured.** Fitting the SAME instrument with the SAME code on
different six-year windows, the estimated once-in-six-years level for SPY ranged from
2.22% to 6.78% — a factor of three, and a factor of four for XLF and IWM — purely
according to which six years the window contained. That was printed as a bare phrase with
no interval on it, and it was wrong in a consistent direction: the top rung fired 1.75
times as often as its own words promised. Hydrology, which invented the method, never
publishes a return level without a confidence interval.

**Why a record is exactly calibrated.** For any distribution whatever, the probability
that the newest of N observations is the largest of those N is exactly 1/N. So "the
largest in the trailing six years" happens about once every six years by construction —
not because a model was fitted well, but because there is no model. Measured on the
archive the record rule delivers 1.21 per six years against the fitted ladder's 1.75, and
29 records against 23.8 expected is within Poisson noise of exact. End to end the
absolute ladder now reads 0.73 / 1.09 / 1.05 / 1.18 across the four rungs.

**Read the ladders separately, never their union.** Each ladder makes its own claim and
each is separately calibrated; a message is the union of the two, taking whichever rung is
rarer, so its rate is roughly their sum — 2.03x at `extreme`. That is a VOLUME figure, and
volume is what `sensitivity` turns. Reading it as a miscalibrated rung is a mistake made
once already in this repository.

**Overclaiming is now structurally impossible.** An instrument cannot be "the biggest in
six years" until it has six years, because the answer is a lookback into a record that does
not exist yet. The old code needed an explicit EXTRAPOLATION_LIMIT for that; the rule is
now the shape of the arithmetic.

**The top rung is six years because six is inside the five-to-ten the recipient asked for,
and for no other reason.** It used to be argued for on the grounds that seven "would
silence the top rung for eighteen instruments at once" — the boundary set to fit the shape
of the archive, so the meaning of the word depended on how much history had been
downloaded and deepening an instrument would quietly rename moves already sent.

**What is lost.** A record is coarser than a fitted level: it says a move beat everything
in six years, not by how far, so ordering within a tier needs the magnitude alongside.
And records cluster — a crisis produces several in a week where a fitted level would have
spread them — which is a true property of markets rather than an artefact.

**What is left over is WHEN an instrument's shocks happened, and it is not fixable.**
After the floor, the six-year rung still fires 2.4x too often for the currency pairs and
about 0.34x for equity and credit. That spread is not an estimator fault and five
candidate causes were measured and cleared: it is not the exceedance cap (uncapping moves
EUR/USD's settled level 0.4%), not the cooldown counted in an asset's own bars
(de-clustering at 72 calendar hours takes FX from 57 events to 56), not tail fatness (FX
scores 1.42 on q99.9/q99, the same as rates and credit and below crypto's 1.55), not the
extrapolation limit, and not the estimator — held at a CONSTANT full-sample level, FX
exceeds about as often as it should, 4 against 3.8 expected for USD/JPY.

It is the causal fit lagging the truth, and the direction of the lag is set by when an
instrument's worst moves fell in its own life. Measured as the median position of each
instrument's twenty largest moves, 0 being the start of its history and 1 the end:

    FX        0.74   level climbs 1.24x   fires 2.43x
    energy    0.70                1.22           1.35x
    rates     0.53                1.08           1.36x
    equity    0.42                0.97           0.34x
    credit    0.37                0.89           0.34x

Correlation with over-firing, 0.543. The currency pairs' worst hours are the 2015 franc
unpeg, Brexit and the 2022 yen - all in the last quarter of their history - so every fit
before them was set on a market that had not yet shown what it could do. Equity and
credit carry 2008 in their first half, so their bar was set high early and the years
since look quiet against it. No fit that refuses to look forward can know which of the
two it is in, which is the price of refusing, and it is worth paying.

Partial pooling of the shape toward a basket-wide prior was measured as the one remaining
lever and does not help: median |log err| 0.758 to 0.743, currency pairs unmoved. The
floor at zero already does what a prior near +0.039 would.

---

## The residual

**The market model, with an estimation gap.** `r = alpha + beta·F + e` on a rolling
500-bar window ending three bars early. Three bars because the leak the field worries
about is short, and three of five hundred does not measurably move the coefficients.

**A second regressor: the instrument's own block factor.** Measured, it earned its
place — but only after a bug: the block factor was computed without orienting members by
sign, so within FX the dollar-quoted and dollar-based pairs cancelled each other and the
factor was near zero. Oriented, it works.

**Leave-one-out in the BMP denominator**, which the published form does not do. In an
ordinary event study every firm shares one event date and the statistic is about their
average. Here each instrument is tested individually against its peers, so a genuine
single-asset move would otherwise inflate the very spread it is measured against — the
instrument would raise its own bar and hide itself.

**It was never a z-score.** Dividing by a *sample* spread over as few as five peers
makes a t, not a z, and the tail was being read off the wrong distribution. The fix was
not a floor on the denominator — it was Wallace's normalising transform, which maps t to
z exactly for the degrees of freedom actually present.

**But S ≈ 1 is a testable prediction, and sometimes it fails.** Over all scored hours the
cross-sectional spread has a median of 0.963, as BMP says it should. In 0.9% of hours it
falls below 0.3 — the whole block asleep, every residual an order of magnitude smaller
than its own long-run sigma predicted. Dividing by 0.05 turns a raw z of 0.86 into 4.05.
EUR/USD, the evening before Independence Day 2006: a **+0.06%** move, pushed as
`extreme`.

**The fix came from outside.** Every A/B-testing and monitoring shop requires *practical*
significance beside statistical significance. And the event-study literature names our
exact failure — Campbell & Wasley found the standardised test misspecified for thin
trading, because near-zero returns corrupt the variance estimate it needs, and recommend
a non-parametric rank test, which estimates no variance at all. We already computed
Corrado's. So: an hour claimed by the abnormal channel **alone** that the rank test
contradicts has that claim withdrawn. No new constant.

That clause is load-bearing. The rank test is itself misspecified when variance jumps —
which is exactly when the absolute channel fires — so the gate lifts precisely where the
rank test stops being trustworthy. Of 24 pushes in October 2008 it removes one; of 15 in
March 2020, none.

**A quorum on peer count is the wrong fix**, and measurement says so: fewer than ten
peers covers **37.5% of all scored hours** — not an anomaly but the shape of a
twenty-four-hour basket whose equities trade six and a half. The thin hours are where
the best calls live: the SNB unpegging the franc, the yuan devaluation, Brexit,
post-Fukushima, the 2024 yen intervention. All fired with nine peers or fewer, and all
have a cross-sectional spread well *above* 1. The discriminator is not how many peers
there were but whether they were moving.

---

## What counts as an event

**A one-cent move is not a small event, it is an unobserved one.** SHY at a $0.01 tick
produced `extreme` alerts on 0.025% moves — the price had moved one tick and the ladder,
fitted on a distribution mostly made of one-tick jitter, called it historic. Anything
under two ticks is now dropped.

**The tier came from one bar and the magnitude from another.** An event that escalates
inside its cooldown keeps the higher tier, but reported the *opening* bar's move beside
it — pushes reading "biggest move in 3 years, +0.01%". The whole bar now moves with the
tier.

**And an event that opens at the top could never move again.** `extreme` is the top of
the ladder, so the strictly-higher rule never fired for it. On 2015-01-15 the Swiss franc
peg broke: USD/CHF opened `extreme` at −3.5% at 09:00 and did **−10.5%** at 10:00, and
the alert quoted the first. The peak now also moves on a bigger move at the same tier,
ranked by exceedance so the two channels stay comparable.

---

## Who gets interrupted

**Rarity and urgency are different questions.** Rarity is a property of the instrument
and means the same whether the system watches five instruments or fifty. How often
someone is willing to be interrupted is a property of the person and does not grow when
the watchlist does. Keeping them apart is what stops the alert rate tripling the day
three instruments are added.

**Not every big move stays.** A move given back within hours is not news, and there is a
cheap way to know which: wait and look. Of events still standing when their own day closed,
80% were still standing at the next day's close, against 26% of those that had already given
it back. The answer is worth having — but not worth waiting for. Both push tiers go out at
once, because a once-in-three-years move that arrives six hours late is a worse product than
one that arrives now, and every instrument in the message is then edited in place at **this
day's close** and **the next day's close** with how its own move actually held. Neither is a
bar count: six bars is most of a session in an ETF and a quarter of a day in crypto, and
neither is a moment a reader can picture. The retention check did not go away; it moved from
deciding whether to send to deciding what the sent message says.

**One event, one interruption, one day.** The push opens a collector that fills until
midnight UTC: another instrument moving before then joins that message rather than buzzing
again, and the next interruption becomes possible with the first bar of the new day. A
calendar day rather than a rolling twenty-four hours because the reader can then say when
the next one can come; UTC because their own midnight is 21:00 UTC, the busiest hour of the
American session, where 00:00 UTC is as quiet as any hour gets. Priced at 26.9 pushes a year
against 25.0 for the rolling window. 2008-11-20 sent six pushes across two hours — SPY, XLF,
USO, then QQQ, IWM, TLT — one market event delivered as six separate buzzes. Pushes
inside a 24-hour window now collapse into the first, **unless a later one is rarer**, and
the survivor names the instruments it speaks for. Naming rather than counting, because
"six others moved" says something happened and nothing about what, and which instruments
moved together is the diagnosis.

**The system does not count its own alerts.** A weekly push cap was removed. A detector
that goes quiet on the third alert of the week is answering a question about the reader's
patience with an instrument's price history, and the failure mode is adversarial: the
week the franc is unpegged is precisely the week a budget starts silencing things,
because that is the week pushes cluster. Volume is controlled where it is generated — by
the ladder, and by the collapse, which asks whether this is the same event as the last
and never how many have gone out.

**Say what the move was big compared with.** "+0.13%, biggest move in about three years"
reads as a bug, and 45% of pushes carry a number under 1%. Short Treasuries move 0.024%
in a usual hour, so it really is six times normal — the message just never said so. Both
numbers are shown, not just the ratio, so the claim is checkable.

---

## How it is scored

**Precision is the wrong yardstick, and here is the proof.** Delaying every alert by six
hours *raises* precision, from 52.0% to 56.0%. No predictive measure can behave that way.
Precision here answers "was the alert about something real"; it credits coincidence as
much as prediction.

**Recall on the obvious is the right one.** Of the hours in the top 0.01% of an
instrument's *own* distribution, how many reached the reader? It should be 100%, and the
counterpart — how often a below-median hour fires — should be 0%. Neither needs episode
labels or a threshold anyone argues about.

Measured, excluding crypto, 152 such hours over twenty-two years: six had no ladder
fitted yet (the instrument too young), and of the remaining 146, **none were silent**.
49.3% push and 50.7% digest, none silent. Below-median hours open an event 0.008% of the
time.

Two traps had to be cleared first. Close-to-close returns span the overnight gap the
detector excludes, so the move being scored was partly something it never claimed to see.
And matching an hour against an event's opening-to-peak span counts continuation hours as
misses — negative oil at 18:00 on 2020-04-21 scored as missed because the push went out
at 16:00, which is the collapse working. The cooldown window is what "would I have found
out" actually asks.

**The published score measured a detector nobody receives.** `tremor.evaluate` scored the
SI-Index cluster channel against the §7 label and that table was, for a long time, the
only one in the report — so "we barely beat the SPY rule" (F1 20.8% against the
baseline's 21.6%) was read off a channel that is computed, written and delivered to
nobody. `price_monitor` reads `saed_events.parquet`. The 8,838 events that reach a phone
had never been scored at all. `tremor.saed_score` now scores them, and the report leads
with it.

**The delivered detector is scored on the claim it makes, not on §7's.** §7 asks whether
a big move follows in the next 24 hours. SAED does not forecast; it says the move that
just happened was unusual for this instrument. Three measurements instead: whether a tier
fires as often as its words promise, whether the moves it sent clear a plain full-sample
quantile at that rate, and which large moves it missed.

**The return periods are wrong, and by how much is now on the record.** Against the rate
each tier claims: `noticeable` 0.45x, `high` 0.88x, `major` 1.41x, `extreme` 1.75x. A
message saying "about once in 6 years" describes something that happens about once in
three. `noticeable` firing at less than half its claimed rate is partly the size floor
doing its job, so that one is a floor rather than a fault; the two rare tiers have no
such excuse, and the `absolute` ladder is where it comes from — its precision at `major`
and `extreme` is 19% and 26% against the `abnormal` ladder's 56% and 52%.

**Each event must be judged on the quantity its own ladder scores**, and getting this
wrong produces a confident table that means nothing. `absolute` scores the raw return,
`abnormal` scores the BMP-standardised residual. Judged on raw size the abnormal events
score 3.5%; judged on the residual the absolute ones score 1.1%. On their own quantities
they are 76% and 84%. Both wrong figures look like findings and are arithmetic.

**Recall is reported twice because the plain figure is not the honest one.** Against every
large raw move, 56-69% per episode. But a large move its block fully explains is supposed
to be silent — that is the fault the system was built to fix — so the figure that matters
keeps only episodes that were large AND unexplained: 68-87%.

**A flat threshold cannot reach 100% by construction**, which is why an earlier "89.7% of
5%+ moves" figure was meaningless: 5% is a quiet hour in SOL and an apocalypse in SHY.

**The original question is close to unforecastable.** "Will some block exceed its 99th
percentile 24-hour move" scores R² 0.02 walk-forward. The same machinery predicting
basket *volatility* scores 0.20 against a naive 0.10. Four phases of work pointed at that
being a property of the market rather than of the code.

---

## The data

**Unadjusted series, ex-dates flagged.** Adjusted series are recomputed retroactively on
every payout, so in a store appended hour by hour the old bars carry one coefficient and
the new ones another — and at the seam an artificial jump the size of the dividend
appears, in an arbitrary hour rather than at a session boundary. Worse than the problem
it solves. The ex-date is derived from the ratio of the adjusted and unadjusted series,
which is a step function, and only the gap channel is masked.

**A day is not a unit of completeness.** Gap detection asked whether a *day* was present.
An hour missing from the middle of a present day was invisible: 333 such hours, found
only once the check moved to the hour.

**Dukascopy reaches 2003 and has four ways to mislead you.** The archive is LZMA-alone
rather than xz; records are 24-byte big-endian `>iiiiif`; the month in the URL is
zero-indexed; and the point size differs by instrument, so a JPY pair decoded with the
default divisor is off by two orders of magnitude. Bid and ask are separate files and
must be averaged. Rows with zero volume are filler, not quiet hours.

**Session holes can be the provider's, and that is provable.** The 2020 gaps were
re-requested day by day; an empty answer for a day the NYSE calendar has is the
provider's hole, not a fetching bug.

**One calendar source, read honestly.** Investing.com (Cloudflare), FRED (US only, no
forecast/actual pairing) and a Kaggle export (starts too late, wrong columns) were each
tested and rejected with the measurement. The archive reaches 2007 from the source
already in use, 91,544 events.

**A vendor's dividend adjustment breaks across a share split, measurably.** XLK, XLY,
XLE, XLU and XLB split 2:1 on 2025-12-05. Twelve Data's `adjust=all` series divides the
NOMINAL pre-split dividend by the SPLIT-ADJUSTED price, so every dividend step before
that date on those five reads exactly twice its true size - verified to three decimals
on six ex-dates against published amounts, while SPY, which did not split, is exact
throughout. The ratio method cannot see the split itself: both series are split-adjusted,
so it cancels, and `SPLIT_THRESHOLD` has never fired. Consequence: `load_steps` is used
only to un-adjust HF Data prices for deepening, where the error is 504-2705bp against
`verify_alignment`'s 25bp tolerance - so the check refuses the import rather than
corrupting the store, and those five carry six years of history instead of twenty-four.
Not yet fixed; the fix is a third series at `adjust=none`, whose ratio to the default
reveals the split factor.

**The corporate-actions table stops at late October 2006, and it does not matter.**
`acquire_since` is 2002 but the request also carries `outputsize=5000`, which caps the
reply at the most recent 5000 daily rows - about 19.8 years - so roughly 375 ex-dates
between 2002 and 2006 are missing. The cost is nil: an ex-date's price drop lands in
`r_gap`, and `r_gap` is computed, stored, and read by nothing. Every detector consumes
`r`, which on a session-open bar is `log(close/open)` - inside the bar. The same fact is
why a future split cannot fire a false alert either.

**Versions hash content, not timestamps.** A re-download that changes nothing must not
invalidate a calibration, and an edited threshold must invalidate it even if the file's
date is untouched.

---

## The shape of the repository

**Derived data is not tracked.** Metrics and residuals are 400 MB rewritten in full on
every run. Tracked and committed hourly that is 4.9 GB a day of new blobs for no new
facts, and the 5 GB repository cutoff arrives within a day. They rebuild from the bars in
about two minutes, which the hourly job does anyway.

**Bars and event tables commit once a day, not hourly.** Measured: git deltas Parquet
well, because appending leaves the earlier row groups byte-identical — a day of new bars
across all 24 files costs about 1 MB, so 0.36 GB a year. Hourly commits would have been
24 times that for the same information.

**Two stores, not one.** Parquet for the columnar history, SQLite where a transaction is
wanted. PostgreSQL would need hosting, and nothing here needs a server.
