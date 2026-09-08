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

**Peaks-over-threshold, not empirical quantiles.** A once-in-three-years level over five
years of history rests on one or two observations. A Generalised Pareto fit above a high
threshold extrapolates from the fitted tail. Probability-weighted moments rather than
maximum likelihood: closed form, so no optimiser to fail, and the better estimator for
small tail samples.

**One percent of the sample as the tail, between 50 and 1000 points.** Checked against
Student-t(4), a harder case than the GPD itself: the three-year level came out 13% low at
200 tail points and within 2% at 600. Below a few hundred, the variance of the shape
estimate dominates the bias it was meant to remove — and a level 13% low fires nearly
twice as often as its nominal rate.

**A tier cannot outrun its history.** "The largest in three years" cannot be said on two
years of data. Fitted at the two-year mark, the three-year level produced eight
`extreme` events in one month across the basket — a burst sitting exactly on the warm-up
boundary and nowhere else.

**Refit monthly, applied only forward.** A full-sample fit would label a 2016 move using
the knowledge that 2020 was coming.

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
