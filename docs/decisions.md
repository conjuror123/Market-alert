# Why it is built this way

> **Holds** the choices that are easy to second-guess, so they are not re-litigated:
> the rule now in force, and the number that settled it.
> **Does not hold** how it works (`architecture.md`), how to run it (`operations.md`),
> or what is still open (`concerns-for-later.md`).
> **Add an entry when** evidence settles a choice. State the rule, then one line of what
> was measured. Not the alternatives, not the story.

---

## The ladder

**A rarity, not a score.** "The biggest move since March 2020" needs no calibration
intuition; a 1-to-100 importance score does. The rung is a SIZE and the message is a
DATE — two questions, answered separately, because conflating them produces a message
that contradicts itself.

**A size, not a fit.** Fitting a tail at this sample size does not work: the same
instrument, same code, different six-year windows put SPY's once-in-six-years level
anywhere from 2.22% to 6.78%. A rung is instead a multiple of the instrument's own
long-run sigma, set per block (`tremor/severity.py`).

**The rungs are a preference, and the rate is an output.** There was once a frequency
guarantee — a rung was literally the biggest move in its own lookback, which is exactly
calibrated because the newest of N observations is the largest with probability 1/N. It
was retired because a rank is relative to a window: after a crash nothing can reach the
top rung until that crash rolls out, and 395 moves larger than a typical `extreme` went
out as something milder, dated March 2020, October 2008 and the 2015 yuan devaluation.
Size is monotone and cannot do that. It also has no 1/N argument, so the frequencies are
measured rather than promised — per instrument, every 2 months, 6 months, 17 months and
2.9 years. The two rungs that interrupt fire about twice as often as the old wording said.

**Set per instrument, never pooled.** Pooling puts SHY and SOL back on one yardstick,
which is the thing the ladder exists to avoid.

**An instrument cannot overclaim.** It cannot be "the biggest in six years" until it has
six years. No extrapolation limit is needed; it is the shape of the arithmetic.

**The top rung is six years** — inside the five-to-ten the recipient asked for, and not
tuned to the archive, because fitting the boundary to whatever history happens to be
downloaded would let deepening an instrument quietly rename moves already sent.

**Read the two ladders separately, never their union.** Each makes its own claim. A
message takes whichever rung is rarer, so the message *rate* is roughly the sum — 2.03x
at `extreme`. That is volume, which `sensitivity` turns, not a miscalibrated rung.

**Known limit — when an instrument's shocks happened.** The six-year rung fires 2.4x too
often for FX and 0.34x for equity and credit. It tracks *where in its own life* each
instrument's worst moves fell (correlation 0.543 with the median position of its twenty
largest): FX's worst hours — the franc unpeg, Brexit, the 2022 yen — are all in the last
quarter of its history, while equity and credit carry 2008 in their first half. No causal
fit can know which it is in, and refusing to look forward is worth the cost.

**What a record gives up.** It says a move beat everything in six years, not by how far,
so the message carries the magnitude alongside. Records also cluster in a crisis — a
property of markets, not an artefact.

---

## The residual

**The market model, with an estimation gap.** `r = alpha + beta*F + e`, a rolling 500-bar
regression ending three bars before the bar being judged. Three bars because the leak is
short and three of five hundred does not measurably move the coefficients.

**The regressor is the instrument's own block factor**, oriented by sign — within FX the
dollar-quoted and dollar-based pairs otherwise cancel and the factor reads near zero. A
basket-wide factor does not scale to sixty instruments the way a block factor does.

**Leave-one-out in the BMP denominator**, which the published form does not do. Each
instrument is tested against its peers individually, so without it a genuine single-asset
move inflates the spread it is measured against and hides itself.

**It is a t, not a z.** Dividing by a sample spread over as few as five peers makes a t;
Wallace's transform maps it to z for the degrees of freedom actually present.

**The rank-test gate.** BMP predicts a cross-sectional spread S near 1 and its median is
0.963, but in 0.9% of hours it falls below 0.3 — the whole block asleep — and dividing by
0.05 turns a raw z of 0.86 into 4.05. So an hour claimed by the **abnormal channel alone**
that Corrado's rank test contradicts has that claim withdrawn. "Alone" is load-bearing:
the rank test is itself misspecified when variance jumps, which is when the absolute
channel fires. Of 24 pushes in October 2008 it removes one; of 15 in March 2020, none.

**Peer count is not the discriminator.** Fewer than ten peers is 37.5% of scored hours —
the shape of a 24-hour basket whose equities trade six and a half. The thin hours hold the
best calls (the franc unpeg, Brexit, post-Fukushima), all with nine peers or fewer. What
matters is whether the peers were *moving*.

---

## What counts as an event

**A one-cent move is unobserved, not small.** At a $0.01 tick a 0.025% move in SHY is one
tick of jitter. Under two ticks is dropped.

**An event carries one bar's numbers.** Escalating inside its day keeps the higher tier
*and* that bar's move — otherwise a push reads "biggest move in 3 years, +0.01%".

**The peak moves at the same tier.** `extreme` is the top of the ladder, so a
strictly-higher rule can never fire for it: USD/CHF opened `extreme` at -3.5% on
2015-01-15 and did -10.5% an hour later.

---

## Who gets interrupted

**Rarity and urgency are different questions.** Rarity belongs to the instrument and means
the same whether five are watched or fifty; willingness to be interrupted belongs to the
person and does not grow with the watchlist. Keeping them apart is what stops the alert
rate tripling the day three instruments are added.

**One event, one interruption, one day.** A calendar day rather than a rolling window, so
the reader can say when the next one can come. The UTC boundary because 00:00 UTC is the
quietest hour there is; a local midnight lands mid-American-session.

**Both push tiers go out immediately, and are edited afterwards.** Of events still standing
at their own day's close, 80% were still standing at the next, against 26% of those that
had already given it back — so waiting is informative, but a once-in-three-years move that
arrives six hours late is worse than one that arrives now and is corrected. Retention
decides what a message *says*, not whether it is sent.

**A close reading waits for the close.** "This day's close" is the last bar the instrument
trades that day, and a live store always ends mid-day — so the reading was taken from
whatever bar had just been fetched, and AVAX-USD read "still there" three hours into a day
with twenty-one hours left. The frame cannot tell a finished day from a three-hour-old one;
`sessions.day_is_closed` can, and both readings wait for it.

**A line about the calendar is not a check-in.** A move made in its closing hour has no day
left to hold through, so its ratio is one by construction. The line is omitted rather than
answered in words.

**The sigma window is a dial between accuracy and crisis loudness, not an estimate.**
Scored as a forecast, every instrument wants the shortest window offered — the wrong
question, since `sigma_eff` is already the fast estimator. Scored against a centred
hindsight estimate, the optimum moves with the bandwidth chosen for "local", so it measures
the choice. The two criteria left disagree because they are one quantity with the sign
flipped: a short window keeps the multiple comparable across eras and goes quiet in a
crash, a long one is the reverse. The setting sits on the loud side, which is the right
choice here and had never been made.

**Exponential weights, not a box.** A box counts a bar from two years ago as much as this
morning's and one an hour older not at all; that edge travels through the data and stepped
the yardstick 16.8% on a twenty-sigma bar. At matched loudness the exponential form holds
the multiple steadier era to era on 82% of equity settings and 65% of crypto ones. It is
cut off at six half-lives, where the measurement stops improving, and normalised by the
weights actually used — so a warm run still reproduces a cold one exactly.

**The half-life is per trading calendar.** One bar count meant 290 calendar days of memory
for an ETF and 58 for a coin, a five-fold spread that fell out of exchange hours. 600 bars
for `us_equity`, 1,400 for `fx_continuous`, 2,000 for `crypto_24_7`, 83 for a daily series
— 86, 82, 83 and 83 trading days, the band the volatility literature settles on. Equity's
worst-year spread of the rarest-1% marker goes 3.02x to 2.00x; crypto gains six points of
coverage in its own worst weeks. The floor cannot exceed the span, or the count inside the
window never reaches it.

**Crypto's window is not extended to match the ETFs' calendar span.** Volatility half-lives
are ~97 days for crypto and 122 for equity, so a flat bar count does give the ETFs more
memory — but crypto's storm coverage saturates at the current span (42% at 5,000, 8,000,
13,000 and 20,000 alike) and era drift gets *worse* for seven of the nine coins, which is
the thing a longer window was meant to fix.

**The system does not count its own alerts.** No weekly cap. A detector that goes quiet on
the third alert of the week fails adversarially: the week the franc is unpegged is exactly
the week records cluster. Volume is controlled where it is generated.

**A closed note is a record, not a feed.** Every tracked note is re-rendered each run, so a
late event can appear — but past `DIGEST_GROW_AFTER_CLOSE_HOURS` it may be corrected and
may not grow. Without that bound one cold rebuild grew a note that had closed two days
earlier from 5 rows to 19 and posted the difference as two alerts at breakfast. The rows
were right; the interruption was not.

**Say what the move was big compared with.** 45% of pushes carry a number under 1%, and
"+0.13%, biggest move in about three years" reads as a bug — short Treasuries move 0.024%
in a usual hour. Both numbers are shown, so the claim is checkable.

---

## How it is scored

**Precision is the wrong yardstick.** Delaying every alert by six hours *raises* it, from
52.0% to 56.0%. No predictive measure can behave that way.

**Recall on the obvious is the right one.** Of the hours in the top 0.01% of an
instrument's own distribution, how many reached the reader? Its counterpart — how often a
below-median hour fires — should be 0%. Neither needs episode labels or an arguable
threshold. Current figures are in `README.md`.

**Judge each event on the quantity its own ladder scores.** `absolute` against the raw
return, `abnormal` against the standardised residual. Swapping them scores 3.5% and 1.1%
and means only that they were swapped.

**Recall is per episode, not per hour.** One shock spans several bars and the detector
reports the peak, so a per-hour figure would mostly measure the debounce.

---

## The data

**The store is unadjusted, with ex-dates recorded.** Adjusted series are recomputed
retroactively on every dividend, so a history built from them changes underneath the record
the ladder is made of. The ex-dividend drop happens between sessions, and nothing between
sessions is a return here.

**Corporate actions are declared, not inferred.** `divCash` and `splitFactor` come from
Tiingo's daily endpoint. Inferring a step from the ratio of adjusted to unadjusted cannot
see a split at all — both series are split-adjusted, so it cancels — and a vendor dividing
a nominal pre-split dividend by a split-adjusted price reports every earlier step at twice
its size. Splits are recorded but **excluded** from un-adjustment: the store is already
split-adjusted.

**The newest rows of the metrics store are never trusted.** The run fires five minutes past
the hour and stores two to thirteen per cent of that hour's volume. Bars heal on the next
fetch; metrics did not, so every hour was judged on its first five minutes — 2026-09-16
18:00, the FOMC statement, went in as SHY +0.02% when the hour closed at -0.19%, taking
thirteen instruments' events with it. `extend_asset_metrics` re-scores its last two days
rather than trusting them, which costs nothing.

**The shard being appended to is the only one whose size matters.** Git cannot delta
parquet, so recording one hour costs the size of the shard it lands in. Sharding by year
does not fix it — the live year's shard grows all year, making the annual bill 183 times
one complete year: 955 MiB to record 5.2 MiB of bars. Settled years keep one shard each and
the live year is split by month. Which year is live is read off the data, not the clock.

**A day is not a unit of completeness.** Gap detection asks about hours: a day present with
three of its seven hours is a hole a day-level check cannot see.

**One provider per instrument, chosen by measurement.** Each candidate was compared against
the stored bars hour by hour in basis points, against one sigma of an hourly move (20-40
bps). A feed disagreeing by a few basis points on a thin fund is not a cheaper feed — it is
a source of alerts for moves that did not happen.

---

## The shape of the repository

**Derived data is not tracked.** Metrics, residuals and the event table are several hundred
megabytes rewritten every run and rebuild from the bars in about two minutes, which the
hourly job does anyway.

**Bars commit once a day, not hourly.** Appending to Parquet leaves earlier row groups
byte-identical, so a day of new bars costs about 1 MB; hourly commits would be twenty-four
times that for the same information.

**Two stores, not one.** Parquet for columnar history, JSON for state where a whole-file
rewrite is the point. Nothing here needs a server.

---

## Settled and closed

Raised, dealt with, and not to be raised again.

- **Retention as a gate** — refused. Gating buys silence for as long as the answer takes.
- **A watchdog for the trigger's silence** — not this repository's job. cron-job.org makes
  the call, so it is the party that knows the call stopped; anything inside the run shares
  a failure mode with the run.
- **`price_monitor/fxcm.py`** — deleted. Covered seven of eight pairs and spliced history
  blind where Dukascopy gates on a measured overlap.
- **`tremor/volume.py` and the `v_r` column** — deleted. Computed in every metrics build
  and read by nothing.
- **The gap channel, `r_gap` and `gap_masked`** — deleted, with the `load_actions()` lookup
  that existed only to feed the flag. The split stays; keeping the discarded half did not.
- **VIX refetched from 1990 every run** — fixed. The stored parquet is read first.
- **FX fetched into a closed market** — fixed. The skip guard takes its expectation from
  the same session walk the bar loop uses.
- **Health messages in the product channel** — fixed. `TELEGRAM_HEALTH_CHAT_ID`.
- **`data/tremor/evaluation.md` and its entry point** — deleted. It scored the SI-Index
  cluster channel, not the delivered detector, against a forecasting label neither claims
  to answer. `tremor/evaluate.py` went with it: its episode and cooldown helpers had no
  caller outside their own tests, because `saed_score` carries its own. Three constants
  in `tremor/windows.py` are now unreferenced and stay there, marked — removing them
  moves `config_version` and rebuilds every metric cold for no change in behaviour.
- **`schema/event_export.schema.json`** — deleted, with its `jsonschema` dependency.
  Nothing produced the export and no test validated it, despite a comment saying one did.
- **299 citations of the deleted specification** — removed across 46 files.
- **Run health measured by a throwaway script** — replaced by `tools/run_health.py`, which
  counts hourly slots with no successful run rather than failed runs, because an outage
  produces none of the latter.
