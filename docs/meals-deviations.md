# Departures from the MEALS 5.1 specification

A list of divergences between the specification and the implementation, each with
its reason. The spec requires departures to be recorded explicitly in several
places (§6.3: changing parameters retroactively without recomputing the history is
forbidden), so this file is kept alongside the code rather than in correspondence.

All of them arose in phase 0, while checking what data is actually available,
rather than out of implementation convenience.

## 1. A fifth block, `crypto`

**Spec:** §2.3 and §8.1 define a closed list of blocks — equity, rates, FX,
commodities.

**Implementation:** a fifth block `crypto` was added (BTC, ETH, SOL).

**Reason.** The basket is built on ETFs: futures are available neither from Twelve
Data on the current plan nor from Yahoo with history deeper than April 2024. An
ETF's trading session is 6.5 hours, so outside the US session the basket holds
only currency pairs — one block. The hourly quorum of §2.3 requires at least eight
assets and at least two blocks of two assets each, so without a fifth block every
hour outside the NYSE session would come out `no_quorum`: no SI-Index, no basket
factor `F_t`, and without the factor no residuals either, so SAED would fall silent
too. The system would be blind for about 17 hours a day.

With the `crypto` block, six currency pairs and three crypto assets remain in
session at night — nine assets against a minimum of eight, two blocks, three Tier-1
assets. The quorum holds, with a margin of exactly one invalid bar.

**What this changes in the calculations:** `N_blocks` in the §2.3 weight rule is
five rather than four; block breadth (§4.2, §5.2) is counted against five
represented blocks by day and two at night.

## 2. VIX — a daily series with a publication lag

**Spec:** §4.4 assumes the VIX series is processed by the §3.1 machinery on the
same footing as the hourly asset series, and that the multiplier window is counted
from `T_spike` — the closing moment of the hourly bar on which the spike was
recorded.

**Implementation:** the source is FRED, series `VIXCLS`. The spike is identified on
DAILY bars. The multiplier window is counted from the moment the value became known
to the system, not from the observation date.

**Reason.** No available source has hourly VIX at the depth needed: Twelve Data's
plan does not include indices, and Yahoo's `^VIX` is live but its hourly history
stops at ~730 days and does not cover the 2021–2023 train period. FRED has no
intraday volatility series at all — every CBOE series was checked, all of them
`Daily, Close`. In exchange FRED gives the real index from 1990 from the official
source, without the contango drift that afflicts any ETF on VIX futures.

The value is published on the morning of the next business day. FRED's
`realtime_start` field for this series is backdated — it equals the observation date
itself — so it is unusable as a publication date, and the moment of availability is
computed explicitly (`meals.fred.available_at`). Without that the backtest would
apply the multiplier in an hour when the value did not yet exist.

**What this changes:** the 24-reference-hour `M_VIX` window in effect covers not the
day after the spike but the day after the spike became known. The loss is moderate:
a heightened-stress regime lives for days, so a window shifted by one day still
lands in the tense hours.

## 3. A shared hourly grid instead of the source's grid

**Spec:** §1.2 sets a single time convention but tacitly assumes that every
instrument's bars sit on the same grid.

**Implementation:** ETFs are requested as HALF-HOURLY bars and folded into hourly
ones on the round UTC hour boundary (`meals.bars.to_hourly`).

**Reason.** The sources' grids do not coincide: ETF bars run on the :30 (09:30,
10:30, …), currency pairs and crypto on the round hour. The cross-section — the
weighted median `M_t`, CSV, PCA, the correlation matrix — measures synchrony, and
on series offset from one another by half an hour it would measure it wrongly.
Requesting half-hourly bars costs no extra credits: the plan counts requests, not
rows.

**A side effect useful for §2.4:** the first half hour of a session produces an
hourly bar assembled from a single half-hourly one. That is exactly the "first bar
of the session" which §2.4 splits into the gap channel and the intra-hour return,
and it can be recognised by the `n_src` field in the store.

## 4. Yahoo Finance excluded from the composition

**Spec:** §2.1 requires the vendor to be fixed and a coverage audit performed, but
names no specific sources.

**Implementation:** three sources — Twelve Data (ETFs and currency pairs), Coinbase
(crypto), FRED (VIX). Yahoo is not used.

**Reason.** Every instrument taken from Yahoo duplicates something already in the
basket through an ETF: `GC=F` and `GLD` are the same gold, `CL=F` and `USO` the same
oil, `ES=F` and `SPY` the same S&P, `ZB=F` and `TLT`/`IEF` the same long Treasuries,
`DX-Y.NYB` the same currency pairs in different packaging. On top of that Yahoo's
endpoint is unofficial and its hourly history goes no deeper than 730 days, so these
series are unusable for the 2021–2023 train period anyway.

The dollar index is not included as an ETF either: it is almost a linear combination
of pairs already included, and its presence would inflate `PC1_ratio` — the system
would declare synchrony where one and the same thing is measured twice.

## 5. Storage — Parquet and SQLite instead of PostgreSQL

**Spec:** §6.1 requires PostgreSQL, permitting SQLite only for local development.

**Implementation:** bars and metrics are Parquet, one file per instrument; event
and cooldown state is SQLite. Everything inside the repository.

**Reason.** The whole project lives on GitHub Actions and has no external service
at all; an external database would add a dependency and a secret for the sake of
data that sits perfectly well next to the code. The requirements of §6.2 and §6.3 —
idempotency, versioning, recomputation under a new `run_version` — do not depend on
the choice of storage and are met in full.

**A side benefit.** The previous NDJSON store was appended line by line, and a bad
branch merge once silently duplicated a continuous block of 299 hours in it — in all
sixteen history files at once. Found in phase 0 and cleaned out
(`python -m price_monitor.candle_store`, 4789 rows). Parquet is rewritten whole, so
such a merge produces an explicit conflict rather than quiet data corruption — and
`bars.to_hourly` additionally drops per-hour duplicates before aggregation, because
volume is summed and would double on a duplicate.

## 6. A half-tick tolerance in the OHLC consistency check

**Spec:** §2.6 requires checking `low <= min(open, close)` and
`max(open, close) <= high`, and marking a bar that fails `is_invalid`, excluding it
from every calculation.

**Implementation:** the comparison allows a tolerance of half the instrument's price
step.

**Reason.** The source rounds a bar's fields independently and not always to the
same decimal place. Real data contains `close` 92.42 against `high` 92.415 on TLT
and `open` 1.0886 against `low` 1.08862 on EUR/USD — a discrepancy smaller than one
tick, that is, a rounding artefact rather than a broken bar. A literal check would
mark such hours invalid and throw away perfectly sound data.

The tolerance is deliberately narrow and was not tuned to zero the counter: after it
was introduced, 14 genuinely inconsistent bars remain out of 551 thousand (12 on
EUR/USD, 2 on SLV). Those should be rejected — that is exactly what the check exists
for.

## 7. ETF series left unadjusted, ex-dates flagged instead of corrected

**Spec:** §2.1 requires stocks and ETFs to be used as adjusted series.

**Implementation:** the series are stored unadjusted, while dividend ex-dates sit in
the corporate-actions table and the gap of such a day is excluded.

**Reason.** An adjusted series is available from the vendor (`adjust=all` works on
intraday bars too and leaves volume alone), but it is recomputed RETROACTIVELY on
every new payout. In a store appended one bar per hour that produces the worst
possible effect: old bars carry a coefficient computed at some point in the past,
fresh ones today's, and at the seam an artificial jump appears the size of the
dividends accumulated since. And not at a session boundary, where the gap channel
would catch it, but in an arbitrary hour, that is, straight into `r_t`. On HYG that
would give a false move of about 0.4% in a random hour every month.

An unadjusted series behaves more honestly: the drop on the ex-date happens between
sessions and therefore lands in the gap channel (§2.4), which by construction awards
no SI-Index points. In addition such dates are flagged and their gap excluded —
otherwise the distribution of the gap channel itself would be skewed by regular
dividend steps.

The `/dividends` and `/splits` endpoints are not on the plan (403), so the dates are
derived from the data: the ratio of the adjusted series to the unadjusted one is a
step function, and its steps are the ex-dates. The check adds up: equity ETFs came
out with 22–23 payouts over 5.7 years (quarterly), bond ETFs 67 (monthly), and the
gold and silver trusts zero, as they should.

**A refinement to §2.4.** The specification says to exclude the bar entirely. Here
only the gap channel is masked: the first bar's intra-hour return has nothing to do
with the payout, and discarding it along with the gap would throw away sound data
for no reason.

## 8. The PCA window is assembled from hours of one regime

**Spec:** §3.3 requires taking into the window only hours that passed quorum, and
only assets with a valid bar in ALL included hours.

**Implementation:** the window is assembled from hours when the whole basket trades,
not from the last 120 consecutive quorum hours. At night PC1_ratio stays NULL.

**Reason.** The column-completeness requirement is right: an incomplete column makes
pairwise correlations incomparable, each computed on a different subset of time. But
§3.3 assumes every basket asset shares one session, and we have two — ETFs trade 6.5
hours, currency pairs 24/5, crypto around the clock. Any window of 120 consecutive
hours touches the night, when the ETFs are closed, so the completeness requirement
throws ALL the ETFs out of the matrix.

Verified on real data: what remains in the window is exactly six currency pairs and
three crypto assets, in every single window without exception. That is, the synchrony
of the equity, rates and commodities blocks would never be measured — although the
cluster detector exists precisely for it.

Assembling the window from full-regime hours gives all twenty-one assets, comparable
correlations and a PC1_ratio that measures what it should. At night the value is
undefined, and per §3.4 the single-factor trigger rests on compression alone — a case
the specification addresses directly.

Mixing regimes in one series would be worse than not computing it at all: the
synchrony threshold is a rolling percentile of PC1_ratio itself, and on a series
alternating between two different typical levels it would describe the proportion of
regimes rather than an anomaly.

**A consequence for the statistics window.** The span of W_cs stays as specified,
1200 reference-calendar hours, but the requirement is placed on the number of VALUES
inside it rather than rows: PC1_ratio exists only in full-regime hours, and a
1200-hour window holds about 335 of them. Demanding 1200 observations inside 1200
hours is demanding the impossible — under that condition the synchrony threshold is
never computed in the whole history.

## 9. A second regressor: the asset's own block factor

**Spec:** §3.6 defines one explanatory variable — the basket factor `F_t = M_t`. The
quantity `M_block,t` is defined in §2.3 but reserved for truth labelling in §7.

**Implementation:** `r = alpha + beta₁·F + beta₂·F_block + e`, where `F_block` is the
median of the block's returns EXCLUDING the asset itself.

**The reason was measured, not assumed.** On the residual series built per §3.6 it
turned out that in hours when four or more currency pairs fire (101 hours, 556
events), 97% of the time every pair agrees on the direction of the dollar. For crypto
the agreement on residual sign is 100% at the median. These are not independent
idiosyncratic moves — they are one block move that leaked wholesale into the
residuals of all its members.

The mechanism is clear: `M_t` is a weighted median over five blocks, and when one
block weighing a fifth moves, the median of the whole basket barely shifts. A module
meant to catch SINGLE-ASSET moves was firing in whole blocks, systematically.
Threshold calibration does not cure this: raise the threshold and you lose the
genuine single-asset events too, while the bunches stay bunches, merely rarer.

**Excluding the asset itself is mandatory.** Otherwise, in a block of three crypto
assets, an instrument would subtract a third of itself and its own move would partly
vanish from the residual — the same error that §3.6 guards against by estimating beta
on data before the current bar.

**The median is plain rather than weighted**, and that is not a simplification: under
the equality rule of §2.3 all weights within a block are equal.

**The degenerate case.** If the two factors are nearly collinear within the window,
the system's determinant tends to zero and the coefficients fly off to arbitrary
values with opposite signs. In such a window the regression falls back to a single
factor, that is, to exactly the behaviour of §3.6.

**The result on real data:**

| | before (one factor) | after (two factors) |
|---|---|---|
| hours with 4+ currency pairs | 101 | 42 |
| of those with 100% dollar agreement | 97% | 50% |
| hours with five or more messages | 97 | 31 |
| maximum messages per hour | 13 | 10 |
| events in total | 2669 | 2478 |

Agreement fell to chance level — that is, the systematic leakage of the block factor
is gone, and the remaining coincidences look genuine.

**What is still imperfect.** The median as a block factor does not work equally well
everywhere. The FX block's six pairs split exactly in half — three with the dollar in
the denominator and three in the numerator — so on a pure dollar move the block median
is near zero and absorbs it only indirectly. For gold the block beta is only 0.12 and
the residual variance fell only to 96%: a median of oil, silver and a broad commodity
basket is a poor proxy for gold. The natural continuation is a block-specific factor
where the median is known to be poor (for FX that means a dollar index, that is, the
mean of returns oriented to the dollar's direction). Not done yet: the measured gain
from the median is already substantial, and further complication is worth doing after
phase 7, on measured precision, rather than before it.

## 10. The calendar is taken from one source, not the best of several

**Spec:** §4.3 assumes a complete calendar across the whole interval, §7 assumes
calibration on train (2021-2023) followed by a run on test (2024 onwards). Where to
get the calendar, the spec does not say.

**How it is done:** the whole history comes from ForexFactory's own monthly pages,
one source, no key. Third-party sources were all tried and dropped, and the fullest
of them was dropped last.

The temptation ran the other way: splice the deepest source with the freshest and
get more events. That is exactly how the archive was built — Kaggle up to October
2025, ForexFactory after — and it turned out worse than having less data. The
sources agree on `High` and diverge sharply on `Medium`:

| | High | Medium |
|---|---:|---:|
| Kaggle, events per week | 13.0 | **96.9** |
| ForexFactory, events per week | 13.4 | **11.3** |

Ninefold is not a difference in completeness, it is a different boundary between
"important" and "so-so". The archive acquired a seam exactly where one source gave
way to the other, and `M_calendar` acquired one with it: the multiplier was on in
**90.7%** of hours across the Kaggle half and **53.2%** across the ForexFactory half.

For calibration that is worse than gaps. The §7 train period would lie entirely in
the generous half while the work ran on the frugal one: the thresholds would settle
on one regime and be applied in another, and the difference would go into the report
as "degraded quality on test", although the cause is purely in the data. A gap does
no such thing — it understates an hour's weight where there is no data, and that is
visible.

**What changed after the rebuild:**

| | before (Kaggle + FF) | after (FF only) |
|---|---|---|
| events | 100,865 | 28,109 |
| multiplier on, before 2025-10 | 90.7% of hours | 59.3% |
| multiplier on, from 2025-10 | 53.2% | 53.1% |
| multiplier on, train 2021-2023 | — | 57.7% |

The seam is gone: 59.3% against 53.1% is ordinary variation between periods, not a
break between sources.

The yearly composition is even, which the old archive's was not:

| year | High | Medium | Low |
|---|---:|---:|---:|
| 2021 | 552 | 760 | 3484 |
| 2022 | 686 | 663 | 3429 |
| 2023 | 910 | 610 | 3290 |
| 2024 | 934 | 659 | 3311 |
| 2025 | 873 | 628 | 3543 |
| 2026 | 522 | 425 | 2830 |

**What it costs:** an order of magnitude fewer `Low` events — ForexFactory simply has
fewer of them than Kaggle labelled. In substance zero: the §4.3 multiplier uses only
`High` and `Medium`, `Low` takes no part in it at all, and the calendar is read
nowhere else.

**The error in the earlier measurement that kept the seam from being noticed.** At
the first assembly the `Medium` difference was visible and judged immaterial, citing
a measurement: "it does not affect hour coverage — 84.8% before the seam and 83.9%
after". The wrong thing was being measured. What was counted was the share of hours
falling inside anyone's event window, and that saturates to nearly one at any density
above a certain point and is therefore insensitive. The value of the multiplier
itself was not measured — and that is what diverged twofold.

**Resolved in phase 7, see §20.** The multiplier was on in 58.3% of hours with a
maximum of 1.80 — better than the previous 84.6%, but "near an important release" was
still the ordinary state rather than the exception. Measuring it against the §7
yardstick showed how little it discriminated: above one in 59.5% of scored hours but
84.9% of event hours, and it alone decided 75% of escalations. §20 records what was
done about it.


## 11. How ForexFactory is actually read

**The spec** does not specify a source at all — this is an implementation note, so
that next time nobody starts from scratch.

The live feed `ff_calendar_thisweek.json` serves only the current week; there are no
`nextweek` or `lastweek` variants, both 404. The weekly pages
`forexfactory.com/calendar?week=...` are closed off by Cloudflare — 403 and a JS
challenge. That is precisely what created the impression that ForexFactory history
was unobtainable, on the strength of which half a dozen third-party sources were
tried.

**The monthly pages `?month=mar.2026` are nevertheless open.** The data sits in the
markup as ready JSON, and the times in it are unix timestamps, that is, unambiguous:
the timezone ambiguity that ruined every earlier attempt is absent here by
construction. The full history 2021-01 … 2026-09 is 69 requests, not one failure.

Three subtleties, each of which cost time:

- **The page is served to `urllib` but not to `requests`.** ForexFactory answers
  `requests` with 403 under any headers, including a full browser set. The difference
  is in the client's TLS fingerprint, and no headers can argue with it.
- **The `market-calendar-tool` library is no good:** it first hits the internal
  `/calendar/apply-settings` to set the display timezone, and that answers 403.
- **The live feed does not serve `actual` at all** — its output has no such key. The
  actual is read back from those same monthly pages, where 76% of events have it.

**What the data confirms:** 28,109 events over 2021-01-01 … 2026-10-01, not one empty
month, zero duplicates by moment of publication, and US `CPI m/m` has exactly two
times — 12:30 and 13:30 UTC in a 45:22 ratio, that is, 08:30 New York in summer and
in winter. Daylight saving is handled correctly.

**What was dropped and why** — the full list is in the README's calendar section.
Briefly: three ready-made dumps gave 25% shifted copies, Financial Modeling Prep is
paid even on "stable", QuantGist is free for only 30 days, MetaTrader 5 is poorer and
needs a terminal, FRED has no publication calendar, and Kaggle diverged on taxonomy
(see §10).


## 12. For the future: surprise instead of the impact label

The archive carries data that was previously unavailable in usable form: `actual`,
`forecast` and `previous` are filled for 70%, 68% and 71% of High and Medium events
respectively. And they are now filled GOING FORWARD too: the live feed does not serve
`actual` at all, and without the weekly backfill from the monthly pages the whole
archive beyond today would be useless for this idea.

This opens up what an impact label cannot give. At present an hour's weight is decided
by an editorial three-valued grading, and a CPI that comes in exactly on forecast
weighs the same as a miss by two standard deviations — although the market reacts to
them completely differently. The meaningful quantity is the surprise:

    surprise = |actual - forecast| / historical_std(actual - forecast)

normalised per individual indicator, because the spread for employment and for a
price index are not comparable.

`M_calendar` then stops being a function of "how important is this release in
general" and becomes a function of "how unexpected did it turn out to be". That also
removes the problem from §10: a multiplier that is on in 58% of hours discriminates
little, whereas a surprise-based multiplier switches on rarely and to the point.

This should be done after phase 7, on measured precision: first we need to see
whether the calendar multiplier contributes to quality at all, and only then improve
its shape.


## 13. The data fingerprint is over content, not modification time

**Spec §6.2:** a repeat run of the same hour under the same version must not create
duplicates, while late or revised data must enter the recomputation under a new
version. How to compute the run version, the spec does not say.

**How it is done:** `run_version` is a hash of `config_version` and a hash of the
*content* of the raw inputs. The first, cheaper implementation took the file's size
and modification time; it was wrong in both directions at once.

In one direction: a backfill rewrites the file with the very same bars. Same size,
new time — and a recomputation over unchanged data would get a new version every
time, meaning the idempotency the version exists for would not exist at all. In the
other: the vendor corrects a bar without changing the length of the line. The size
is unchanged, and if the file was rewritten within the same second the edit would
slip through unnoticed — exactly the case §6.2 demands a new version for.

The price is a hundred milliseconds per run: there are about thirty megabytes of raw
data. That fits into the hour between runs with a large margin.

**What it costs:** the fingerprint is sensitive to the byte order in a file, not only
to the data. Rewriting Parquet with a different library version or a different
compression level gives a new `run_version` for the same bars. That is a false
positive, but a safe one: an extra recomputation rather than a missed one.


## 14. Derived files are excluded from the fingerprint

**Spec §6.2** speaks of "the same data" without saying what counts as data.

**How it is done:** the fingerprint covers only raw inputs — bars, the VIX series,
the NYSE schedule, the dividend ex-date table and the economic-calendar archive.
Asset metrics, basket metrics, residual series and the decision journal are excluded.

Otherwise reproducibility is impossible by construction. The file
`metrics_basket_hour` is both input and output for the `cluster` run: it reads the
quorum and M_t from it and appends points, multipliers and per-hour decisions to the
same file. Include it in the fingerprint and a second run over the same bars would
get a different `run_version` simply because the first run rewrote the file.

For the same reason the run clears its own derived columns before computing
(`cluster.reset_derived`), and computes the versions before writing rather than
after.

**What it costs:** if a derived file is corrupted by hand, the fingerprint will not
notice. The protection here is different — derived files are rebuilt wholesale by a
command rather than edited.


## 15. The event export and the residual series are not kept in the repository

**Spec §6.5** requires an event export to JSON under a fixed schema. Where to keep
it, the spec does not say.

**How it is done:** the schema lives in `schema/event_export.schema.json` and is
versioned, while the export itself goes to `data/meals/events/`; that directory and
`data/meals/residuals/` are in `.gitignore`.

188 events amount to 9.7 MB of JSON, the residual series to another 32 MB, and both
are rewritten whole on every run: `run_version` stands in every export file, and the
residuals are recomputed on any change of the factor. Forty megabytes per run in git
history, fully derivable from the code and the metrics, is repository growth without
a single new fact.

The asset metrics do stay in the repository, and the boundary is drawn deliberately:
they depend only on the bars and cost a full `pipeline` run, whereas these two
outputs are seconds on top of them — and `saed` comes before `cluster` in the run
order anyway.

**What it costs:** after a clone you have to run `python -m meals.saed` and
`python -m meals.export`, or the decision journal will be assembled without SAED rows
and there will be no export at all. The contract is preserved regardless: the schema
is in the repository, and a test validates against it both a synthetic event and all
188 real ones.


## 16. Per-asset decisions are journalled only where a trigger fired

**Spec §6.1** requires a decision journal covering all triggers.

**How it is done:** basket decisions are written for every hour that passed quorum —
287 thousand rows over five years, tolerable. Per-asset decisions are written only
where a trigger fired.

Twenty-three instruments over thirty-five thousand hours would give some two million
rows, almost all of them saying "nothing happened" — while the values and thresholds
for every hour already sit in `metrics_asset_hour`, from which they are retrieved by
the key (hour, asset). The journal adds exactly one thing to them: the pairing "what
was compared against what, and what came of it", and that is interesting where the
comparison produced a result.

**What it costs:** the absence of a row in the journal means either "did not fire" or
"was not assessed", and the journal alone cannot tell them apart. For basket
decisions the distinction is preserved (an hour without quorum yields no row at all,
and that is documented), and for assets the answer comes from the `first_valid_hour`
table: it holds the first hour from which a trigger is assessed at all, and the share
of assessed hours.

---

## 17. `recalculated` is judged on the data, not on `run_version`

**Spec §6.2** says that when data arrives late or the vendor revises it, the hour is
recomputed under a new `run_version` and the affected records are marked
`recalculated`. **§6.4** lists `recalculated` among the columns of `cluster_events`,
and does not list a data fingerprint there.

**How it is done:** `cluster_events` carries one column beyond the §6.4 list —
`data_fingerprint`, the hash of the raw inputs — and `recalculated` compares that
against the fingerprint stored on the previous run rather than comparing
`run_version`.

The reason is that `run_version` is a hash of `config_version` AND the data
fingerprint, so a row carrying only `run_version` cannot say which of the two moved.
Judging the flag on it raises `recalculated` on every code edit — a moved threshold,
a reworded comment. Measured on the real history: editing a docstring in `saed.py`
moved `config_version` from `c4948ebff206` to `6df97bd2867f` and flipped all 188
cluster events to `recalculated = True` without a single price bar having changed.

That matters most in phase 7. Calibration moves the thresholds on every iteration, so
a flag judged on `run_version` would stand at True on every row for the whole phase
and carry no information exactly when the question "did the data shift under me
between these two runs?" is worth asking. What the code changed is already what
`config_version` is for.

**What it costs:** one string column on `cluster_events`, about 2 KB over the whole
table, and a departure from the literal column list of §6.4 — an addition to it, not
an omission from it.

---

## 18. Three readings §7 leaves open

**Spec §7** defines the truth-labelling protocol and the baseline in one paragraph
each. Three things in them admit more than one reading, and since the labels are the
yardstick every later number is measured against, the choices are written down here
rather than left in the code.

**The accumulated move is compared in absolute value.** §7 asks whether a block's
accumulated 24-hour return runs "in excess of the Q99 of its own historical
distribution of such 24-hour moves". Taken on the signed return, the Q99 of a roughly
symmetric distribution is its upper tail alone, and only rallies would ever be
significant — a market falling apart would be labelled calm. A "move" is a magnitude,
so the comparison is `|accumulated| > Q99(|accumulated|)`.

**The 2% of the baseline is read on the log scale.** Everything in this system
accumulates log returns, because they add and simple returns do not. A 2% move is
therefore `ln(1.02) = 0.019803`, not `0.02`. The difference is a fifth of a percent of
the threshold and changes nothing measurable; it is stated because a reader otherwise
has to guess which of the two was meant.

**The baseline is computed both ways, and the runnable one is the one worth beating.**
§7 puts the baseline detector on "the move accumulated over the FOLLOWING 24 RCH" —
the very window the truth label is built from. Read literally that is not a detector:
at hour `t` it reads hours after `t`, which nothing running live can do, and it scores
near the label by construction, since SPY sits inside the equity block the label
measures. Measured: 44.8% precision against a 3.3% base rate, a 13.6x lift, for a rule
that is close to a second look at the answer.

So `truth_labels.parquet` carries both. `baseline_spy` is §7 exactly as written.
`baseline_spy_trailing` fires on the 24 RCH BEFORE `t` — "SPY has just moved 2%,
expect more" — which is a detector that could actually run, and it reaches 16.4%
precision, a 5.0x lift. That second one is the honest thing to compare the SI-Index
against, and it is the harder target: on test it reaches a 13.8x lift where the
cluster detector at the spec's uncalibrated starting values reaches 0.5x.

**What it costs:** one extra column, and a note that any comparison quoting "the §7
baseline" has to say which of the two it means.

---

## 19. How the §7 metrics are counted

**Spec §7** asks for precision, recall, F1 and lead time, overall and per block, and
names a baseline to compare against. It does not say what a unit of measurement is,
and three choices there move the numbers more than any threshold does.

**Scoring starts when the whole basket is warm, 2021-11-22.** §6.6 keeps an hour out
of backtest statistics while its triggers are still warming up. The last ETF's Q95
window fills on 2021-11-22; before that only crypto and FX are warm, and three cluster
events were in fact created from those two blocks alone. Scoring them would measure a
handicapped detector against a full yardstick. The truth thresholds are unaffected and
still use the whole train period — they are built from returns, which are valid from
the first bar.

**Recall is per episode, not per hour.** A cluster event holds a 72-hour cooldown, and
a 24-hour forward label turns one shock into roughly two dozen consecutive significant
hours. Per-hour recall would therefore mostly measure the cooldown: the detector is
forbidden from firing on hours it has already reported. Contiguous significant hours
are collapsed into one episode, and the question is whether the episode was caught at
all — which is also the question a person receiving the alerts would ask. A detection
is credited to an episode if it lands within 24 reference hours before its start, or
anywhere inside it, and lead time is measured from the earliest such alert.

**The baseline is given the same cooldown.** The trailing SPY rule fires on 940 hours
against the SI-Index's 184 events. Comparing recall as they stand would reward the
baseline for being allowed to shout, so it is also run through the 72-hour cooldown
and both versions are reported. It matters: uncooled, the baseline reaches 39.2%
recall against the detector's 28.8%; cooled to a comparable 65 alerts it reaches
18.3%, below the detector — while keeping more than double the precision.

**What it costs:** none of these numbers is comparable with one computed per hour, and
an earlier per-hour reading of the same data made the detector look worse than chance
on test. The unit has to be stated whenever a figure from this report is quoted.

---

## 20. The calendar multiplier is tiered by country and its windows are shortened

**Spec §4.3** gives every High-impact release a window of six hours before and three
after with a peak of 1.8, and every Medium one four and two with a peak of 1.5. It
makes no distinction between countries.

**Why that does not work on this calendar.** ForexFactory labels impact PER COUNTRY,
so `High` means high *for that currency*: a New Zealand rate decision carries the same
label as an FOMC decision. That yields **823 High-impact releases a year**. At a
nine-hour window each, that is

> 823 × 9 = 7,407 hours against the 8,760 in a year = **84.5% of the clock**,

before Medium's 631 a year at six hours is counted at all. Overlap is the only reason
the multiplier landed at 59.5% of hours rather than higher. A multiplier that is on
for most hours raises most scores and therefore ranks almost nothing — measured
against the §7 yardstick it was above one in 59.5% of scored hours but 84.9% of event
hours, and it alone decided 75% of all escalations.

**How it is done:** releases are split into two tiers. `USD`, `EUR` and `All` —
ForexFactory's marker for a release with no single country — keep the full peak and
the wider window; every other country keeps a narrower one and a smaller peak. Both
tiers keep the asymmetry of §4.3, more before the release than after, and both keep
its piecewise-linear shape.

| tier | importance | before | after | peak |
|---|---|---:|---:|---:|
| core (USD, EUR, All) | High | 2h | 1h | 1.8 |
| core | Medium | 1h | 0.5h | 1.4 |
| other | High | 1h | 0.5h | 1.3 |
| other | Medium | 0.5h | 0.5h | 1.15 |

Coverage falls from **59.5% to 17.4%** of hours, and from 24.3% to 3.9% at 1.5 or
above. Core-only would have given 12.6%, so the other currencies contribute about five
percentage points — present, no longer dominant.

**What it costs:** the tiering itself is not in the spec; the peaks and windows are
starred there and so are calibration parameters, but the country split is structural.
The choice of which countries are core is a judgement about this basket — it holds
SPY, QQQ, IWM, XLF, TLT, IEF, SHY, HYG, GLD, USO, SLV and DBC, all US-listed and
US-priced, plus six dollar pairs — and would need revisiting for a differently
composed basket.

**Deliberately not done:** replacing the impact label with the *surprise* (how far the
actual came in from the forecast). It is the better measure — a release that lands on
forecast moves nothing — but the archive has a forecast on only 63% of events and an
actual on 75%, so a surprise-based multiplier would be undefined for a third of the
calendar. The intended shape is a supporting factor that switches off where the data
is missing rather than a replacement for the label; that is future work, and §12
records it.

---

## 21. The single-factor trigger is built on coherence, not on CSV compression

**Spec §3.2** defines the compression sub-condition as `CSV_norm` below the 10th
percentile of its own recent history AND `|M_t|` above twice its own sigma. **§3.4**
makes the single-factor trigger the OR of that and `pca_sync`.

**Why that does not work.** Compression fires in **zero** hours out of 29,532. Its two
halves are opposites as written: "CSV below its own 10th percentile" means the assets
barely moved, because that percentile is set by quiet hours, while "|M_t| above two
sigma" means they moved a great deal. Measured separately, the first holds in 9.65% of
hours and the second in 4.68%; independence would predict about 134 hours of overlap
and the actual number is nought.

There is a second fault underneath. CSV is the standard deviation of RAW returns
across a basket whose assets differ in scale by a factor of forty — Solana moves 49
basis points in a typical hour where SHY moves one. So it is dominated by whichever
crypto asset is loudest: **CSV on raw returns correlates 0.904 with Solana's own
|r|**. It is a Solana volatility gauge wearing the name of a cross-sectional
statistic. On Z-scores that correlation falls to 0.365.

**How it is done:** a new quantity, `coherence`, measures what §3.4 is actually after —
the basket moving as one thing. It is the cross-sectional mean of the Z-scores divided
by their cross-sectional spread, a signal-to-noise ratio across assets: every asset at
+2 sigma gives a large mean over a small spread, unrelated wobble gives a mean near
zero. The sub-condition is `coherence` above the 90th percentile of its own recent
history AND the same second leg as §3.2, unchanged. `single_factor` is now
`coherence_compression OR pca_sync`.

It fires in **1.12%** of hours where the spec's version fires in 0.00%, and it is
measurable in essentially every hour where `pca_sync` is measurable in only 28% — PCA
needs every asset to have a valid bar in every hour of a 120-hour window, and the ETFs
are shut for most of them.

**§3.4's correlation check is now computable for the first time.** It requires
dropping one sub-condition if the two correlate above 0.7; with a sub-condition that
never fired the correlation was undefined. It now measures **0.067**, so both survive.
Note the overlap in absolute terms is real — 275 of the 332 coherence hours also carry
`pca_sync`, so coherence contributes 57 hours the trigger would not otherwise have —
and the low coefficient reflects how differently often the two fire, not that they are
unrelated.

**What it costs:** `csv_compression` is still computed, still logged and still exported
as `csv_norm` requires (§6.5), but no longer feeds the trigger. Its empty column is
kept deliberately: §3.4 wants both sub-conditions logged separately, and the emptiness
is the evidence for this departure.

---

## 22. The score carries a graded breadth term

**Spec §4.2** awards fixed points for four yes/no triggers — price shock +3, volume +2,
cluster shift +4, single-factorness +3 — and states that the maximum sum is 12.

**Why that is not enough.** The gate of §4.1 requires the cluster shift, so its +4 is
always present in an event, and volume cannot fire without a price shock. That leaves
exactly five reachable totals — 4, 7, 9, 10, 12 — with `THRESHOLD` sitting between the
first two. So the gate reduces to "cluster shift AND price shock", and 67.8% of
cluster-shift hours already reach 7 on base points alone. "Calibrating THRESHOLD on
train" is then not tuning but choosing one of five operating points: raising it from 7
to 8 jumps straight to requiring 9, that is, volume confirmation as well.

**How it is done:** on top of the flat award for the cluster shift, points are added in
proportion to how broad the shift was — the share of the blocks present in that hour
that are active by Q95, times `POINTS_BREADTH` (4.0, starred). The information was
already computed; §4.2 simply discards it, and a shift across two blocks scores
identically to one across five. The term applies only where the cluster shift fired:
breadth without a shift is a handful of assets moving, which the price-shock trigger
already speaks for.

Reachable `base_points` values go from 5 to **24**, and `si_total` from a handful to
**149** distinct values. A threshold sweep on train now traces a smooth curve — at
THRESHOLD 5 the detector fires 97 times at 23.7% precision and 30.6% recall, at 16 it
fires 22 times at 40.9% and 9.9% — where before there was nothing between the rungs.

**What it costs:** the maximum sum is 16, not the 12 §4.2 states. `MAX_FLAT_POINTS`
keeps the spec's 12 as its own name, since that is what the four trigger weights must
still add up to. Note also that these three changes together did NOT improve the
metrics on their own — precision moved 15.2% to 14.8%, recall 28.8% to 26.1% — because
`THRESHOLD` and `ESCALATION_THRESHOLD` are unchanged and the detector is simply sitting
at a different arbitrary point on its own curve. The point of the change is that there
is now a curve to calibrate along.

---

## 23. The calibration fits four parameters, not thirteen

**Spec §7** lists what is calibrated on train: `THRESHOLD`, `ESCALATION_THRESHOLD`, the
trigger weights, the `V_R` threshold, the coefficients of the absolute legs, `k_t` and
the parameters of the multipliers. Thirteen numbers in this implementation.

**Why not all thirteen.** They are not identifiable from 111 training episodes. Running
the same coordinate-descent search from three different random seeds on the same data
gave three different answers of the same quality:

| seed | objective | `points_breadth` | `points_volume` | `vix_strength` | `threshold` |
|---:|---:|---:|---:|---:|---:|
| 1 | 0.3022 | 0 | 8 | 1.0 | 6 |
| 2 | 0.2761 | 3 | 2 | 1.0 | 7 |
| 3 | 0.3000 | 4 | 2 | 3.0 | 10 |

Two further runs under wider grids landed on `points_breadth` 8 and 10, `vix_strength`
0.0 and 3.0, `threshold` 10 and 16 — again at the same objective. The multiplier that
one run switches off entirely, another triples. What the search reliably finds is that
calibration is worth about ten F1 points over the spec's starting values; which
particular parameter vector delivers them, the data cannot say.

**How it is done:** four parameters are fitted and the rest keep their configured
values — `THRESHOLD` (7 → 13), `ESCALATION_THRESHOLD` (12 → 22), `ABS_LEG_Q99`
(3.0 → 4.0) and `POINTS_BREADTH` (4.0 → 6.0). They are the four §7 names first and the
four with a plain operational meaning: when to open an event, when to escalate, how
extreme a single-asset shock must be, and how much breadth counts.

**The objective is not plain train F1.** Train is cut into three folds of equal episode
count and the objective is their mean F1 less their standard deviation, inside a
declared band of 0.2 to 3.0 alerts a week. Optimising plain F1 instead pushed five
parameters onto their grid edges and *widened* the gap between the halves of train,
0.383 against 0.2243 — the signature of fitting noise. Folds are cut by episode count
because train is front-loaded: halved by time, one side holds 92 episodes and the other
19.

**`ESCALATION_THRESHOLD` was not fitted on the objective**, which scores event creation
and barely sees escalations — they change events only by restarting a cooldown. The
objective is flat from 22 upward. 22 is the smallest value on that plateau, it keeps
escalations alive (7 on train against 1 at 32), and it is within rounding of the spec's
own 12/7 ratio applied to the new threshold.

**What it costs:** nine of the thirteen parameters keep values that were never fitted,
so a claim that this configuration is optimal would be false. It is a configuration
that is defensible and reproducible — fixed seed, published grids, the search recorded
in `data/meals/calibration.json` and the freeze in `data/meals/frozen.json`.

---

## 24. Two matched-horizon channels, a graded label — and what they did not fix

**Spec §4.2** builds the score from four one-hour triggers. **§7** asks whether the
next twenty-four hours are significant. **§8.6** makes the SAED dependency one-way.
All three were changed at once, because §7 leaves one clean test and iterating spends
it.

**The diagnosis, all measured on train so the test stayed clean.**

*The truth label is a cliff.* Of 37 alerts scored as failures, **not one** landed on a
quiet market: the median reached 0.69 of its block's Q99 threshold and 14 came within
25% of it. An alert before a move reaching 0.99 of the line scores as a total failure.

*The triggers answer the wrong horizon.* The same basket move read over 24 reference
hours reached **80%** precision on train where its one-hour form reached **25%**.

*The discarded module predicts better than the detector.* The SAED count over 24 hours
reached **53%** precision at its 98th percentile and **83%** at its 99th, against the
cluster detector's own 37%.

**Two hypotheses tested and rejected.** Precision inside the full-basket regime is
34.3% against 41.7% outside it, so the 28% PCA coverage is not the bottleneck. And the
`si_total` distribution barely moves between periods (q99.5 of 13.80 against 13.00), so
a regime-adaptive threshold would hold the firing rate constant — which is the problem,
not the cure.

**What was built:** `significant_near` at 0.75 of the threshold beside the strict §7
label, which stays what is optimised and reported; `trigger_sustained`, the basket move
accumulated over 24 reference hours scaled by sigma·sqrt(24); `trigger_saed_breadth`,
the single-asset event count over the same window. §3.1's and §8.2's absolute legs were
also separated — the spec states them apart and this implementation had shared one
constant, so calibrating the price leg silently moved SAED's sensitivity, which mattered
once SAED fed the score.

**The calibration liked them.** Both channels were raised above their starting values
rather than driven to zero — `sustained` 3.0 → 6.0, `saed_breadth` 3.0 → 4.0 — and for
the first time no parameter landed on a grid edge.

**They did not fix it.** Second test, frozen at `429462f6b28d`, train ending 2025-01-01:

| | precision | recall | F1 |
|---|---:|---:|---:|
| train | 27.5% | 29.1% | 28.2% |
| test | 6.0% | 18.8% | **9.1%** |
| *first test, for comparison* | 6.8% | 16.7% | *9.6%* |

The softened label confirms the cliff is real — precision 19.9% → **42.6%** against a
threshold at 0.75 — but the generalisation failure is untouched. The mechanism check,
which uses no labels at all, says why:

> episode rate, test/train = **0.41**  ·  firing rate, test/train = **0.98**

The market produced 41% as many significant episodes per hour, and the detector fired at
98% of its former rate. A score that does not thin when the thing it predicts becomes
rarer is not tracking that thing, and adding better-aimed channels to the sum did not
change that. Two independent test periods now agree at ~9% F1.

**What it costs:** the honest reading is that the SI-Index, as specified and as
extended here, does not generalise across volatility regimes on this basket. The
remaining ideas are structural rather than parametric — scoring relative to a rolling
distribution of the score itself, or conditioning on a regime state — and each needs a
test period this one has not spent. The untouched data begins 2026-09.

## 25. Adopt a method, then search for what it excludes

A process note rather than a design decision, written down because the mistake it
describes cost three sessions and was avoidable by one extra search.

**What happened.** The single-asset detector was rebuilt on the event-study
literature: a market model, abnormal returns, BMP standardisation against the
cross-section. That literature was found by searching for how to detect *abnormal*
returns, and it answered that question well. It was then adopted wholesale.

Event studies ask "did this firm react to *this* announcement". In that question the
market-wide move is a **nuisance to be removed** — it is the thing the market model
exists to subtract. So a detector built on it is blind, *by construction*, to days
when everything moves together, which is what a macro event is.

**What it cost.** Measured on this basket, the blindness was total:

| day | what the abnormal channel pushed |
|---|---|
| SVB collapse, Mar 2023 | one ETH alert |
| Yen carry unwind, Aug 2024 (VIX ~65) | nothing |
| US election, Nov 2024 | nothing |

It is also a metronome. Once the full basket is live the monthly event rate per
instrument varies **1.8×** (1.26 to 2.26), because the score has the market's
volatility divided out of it twice — once by the instrument's own rolling sigma, once
by the peer spread — before any threshold is applied. It cannot report that a month
was unusual.

Worse, §8.2's absolute leg was *removed* in `62ffad0` on the grounds that "the
literature adds no second raw-magnitude filter". True of the event-study literature.
The leg was badly built — a fixed multiple of a slow sigma, passing 139.9× more often
in the loudest hours than the calmest — but it was pointing at a real gap. The symptom
and the signal were deleted together, when the fix was to rebuild it properly (§26).

**The rule.** *When adopting a method from a literature, run a second search for what
that literature excludes by construction.* Concretely, after finding event studies,
one search for market-wide stress or systemic-risk measures would have surfaced the
gap immediately — the field keeps a separate literature for it precisely because the
market model cannot answer it.

The generalisable form: a method's assumptions are advertised, but its *exclusions*
usually are not. Ask what question the method treats as noise, then ask whether that
question is one this system needs answered. Here the answer was yes, and nobody
asked for three sessions.

**A second instance, same shape.** The severity ladder assumed a two-sided quantity
centred at zero, because every quantity it had been asked of — a return, a
standardised residual — was one. Pointed at the detector's forecast, a *log
volatility* running from −8.26 to −4.98, it took the absolute value and ranked the
**calmest hours on record as the rarest**. Same failure: a default that was correct
for everything seen so far, silently wrong on the first quantity of a different kind.
Now explicit as `severity.magnitudes(score, two_sided)`.

## 26. Three channels, one ladder, one delivery stream

§8 specified one question — was this move unexplained by the market — and one
critical value shared by every instrument. Both are replaced. What is delivered now
comes from three channels which differ in *what they measure*, all placed on one
severity ladder and routed through one set of rules.

**The ladder** (`meals.severity`). Severity is a **return period**: how long you would
ordinarily wait to see something this large in this instrument. Four rungs — a
fortnight, two months, a year, three years — estimated by peaks-over-threshold, a
Generalised Pareto fitted to excesses above a high threshold and extrapolated (Coles
2001), with probability-weighted moments (Hosking & Wallis 1987) rather than maximum
likelihood: a closed form, so no optimiser, and the better estimator for small tail
samples. Below the threshold the empirical quantile is used; the two agree where they
meet. Fitted per instrument on an expanding window, applied only forward.

This replaces the shared critical value because that value answered a question about
the null hypothesis rather than about the recipient — it made a once-a-decade move in
SHY and a Tuesday in SOL come out identical. It also settles the block imbalance
without a rule for it: FX was 341 of 426 events and is now 847 of 2239 alerts, with
every other block between 334 and 372.

**The channels.**

| channel | asks | of |
|---|---|---|
| abnormal | was this move unexplained by the market | `z_resid_bmp` |
| absolute | was this simply a big move for this instrument | `r`, the raw return |
| market | was the market as a whole disorderly | detector exceedance |

`abnormal` and `absolute` are merged per hour by `severity.combine` into one tier and
a `basis` recording which fired — one event, delivered once. The `market` channel is
`meals.market`, and it re-scores nothing: `meals.detector` already forecasts basket
volatility and marks its own alerts, and what it lacked was any severity, so its
alerts could not be ranked against an instrument's and went nowhere.

**Three things measurement forced, each against the obvious choice.**

*The tail-sample cap went up, not down.* Fitting deeper into the tail seems right and
is not: on Student-t(4) the three-year level came out 13% low at 200 tail points and
within 2% at 600. Below a few hundred points the variance of the shape estimate
dominates the bias it was meant to remove, and a level 13% low fires nearly twice as
often as nominal.

*A tier is withheld until the history can back it.* "The largest in three years"
cannot be said on two years of data. Fitted at the warm-up mark the three-year level
produced eight `extreme` events in the single month where that boundary fell and
nowhere else in five years. Moves clearing a withheld level land one rung down.

*The market channel reads the exceedance, not the level.* The forecast is not
stationary — its level depends on basket composition, and this basket grew. On an
expanding window a ladder on the level never fires again: the routine level settled at
−5.21 during the crypto-heavy warm-up and the forecast topped out between −5.46 and
−5.74 in every half-year since. Zero market events, silently, forever. The exceedance
over the detector's own rolling threshold is stationary by construction. **An
expanding-window return period belongs on a stationary quantity.**

**Delivery** (`meals.routing`). Rarity and urgency are kept apart on purpose: rarity
is a property of the instrument and means the same thing whether the system watches
five instruments or fifty; how often someone will be interrupted is a property of the
person and does not grow with the watchlist.

- `extreme` pushes at once, without waiting to see whether it held.
- `major` waits six bars and pushes only if still standing.
- everything else that held goes to the next **Tuesday or Friday** digest — Tuesday
  covers the weekend and Monday, when crypto trades through and equities gap; Friday
  closes the week. Separate from the Saturday calendar digest, which is a *forecast*
  where these are a *report*.
- moves that reverted are dropped.

Retention (`meals.persistence`) is the cumulative abnormal return over the next *h*
bars divided by the move itself — the permanent-versus-transitory split: liquidity
impact reverses, information impact continues. Measured, the median move retains 0.88
at 24 bars and the tiers order correctly (once-a-year moves held 72% and fully
reverted 14%, against 59% and 31% for once-a-fortnight ones). It is read on the series
matching the event's basis, and is **not applied** to market events at all — a
volatility regime is not a price move and a spike that subsided was still a spike.

**Where it stands.** A push every 27 days; 1.7 items per digest; 2638 instrument
events of which 899 are absolute-only; 3.6 market events a year. Month-to-month
variation improves from 1.8× to 2.8×.

**What is still open.** The August 2024 yen unwind is caught only at `routine`, and
SVB and the 2024 election are not caught by the market channel at all — the detector
itself never fired on them, which is §24's open problem and not a routing one.
Breadth was prototyped as a fourth channel and deliberately not shipped: counting how
many instruments fired thresholds a series that is already rate-controlled per
instrument, so it discards the magnitude and failed to flag the yen unwind even with
the warm-up shortened to cover it. The continuous cross-sectional measure is the right
statistic and the detector already computes it.

## 27. The 2020 session holes are the provider's, and were proved so

Deepening the ETF archive from 2021 to 2020 (§26) surfaced trading days the NYSE
calendar has and the bar store does not — `2020-02-18` in **all twelve** US-equity
instruments, plus a scattering of one to three more per symbol.

A day missing from twelve instruments at once looks systematic, and it sits eight days
after the archive's new start date — exactly where a fetch-boundary artefact would
live. So the question was whether the hole was Twelve Data's or ours, and that is not a
question worth reasoning about when one request settles it.

`meals.backfill --fill-gaps` re-asks the provider for each missing session individually.
Run 33994110137, all twelve instruments:

> **gap fill: 0 recovered, 21 confirmed missing at the source**

Every day, asked for directly in its own window, came back empty. The holes are Twelve
Data's. Nothing our fetching does will close them, and no other free source carries
US-equity **intraday** history back that far (§25's search covered this: Dukascopy's ETF
CFDs are patchy and volume-incompatible, FXCM has no equities).

**What this cost the session test.** `test_table_matches_the_days_the_data_actually_has`
asserted both directions exactly, and the deepening broke one of them. It was replaced
by two tests rather than relaxed into uselessness:

* *The data never has a day the calendar does not* — still absolute, and now checked
  across all twelve instruments rather than SPY alone. A bar on a closed day means the
  calendar is wrong or the bars are misdated, and either poisons quorum and the
  cross-section underneath everything else.
* *The missing sessions stay a handful and none are recent* — bounded at 0.5% of
  sessions (the worst instrument sits at 0.18%), and **zero tolerance inside 90 days**.
  A recent hole is not an old provider gap, it is the live collection failing now.

The second is weaker than what it replaces in one way and stronger in two: it covers
twelve instruments instead of one, and it distinguishes an old archival pit from a
live-collection failure, which the original could not do at all. The evidence for the
tolerance is a run id, not a judgement call.

**The rule this is an instance of.** A test that fails on new data is asking a question,
not making a complaint. Answer it — here, by spending twenty-one API credits — and only
then decide what the assertion should say. Editing the assertion first would have
produced the same green tick and destroyed the finding.

## 28. A day is not a unit of completeness

§27 asked the provider for twenty-one missing sessions, proved they were its holes,
and recovered them from HF Data. It reported itself finished. It was not, and the
reason it could not tell is worth more than the twenty-one days it did fix.

**What the check could not see.** `missing_sessions` compares the set of days the
calendar has against the set of days the store has. A day with one bar in it is in
the second set. So a session holding two of its seven bars is, to that check,
present — indistinguishable from a complete one.

The patched days made this visible by accident. Verifying the quality of the HF Data
patch on SPY, the patched day looked anomalous — three to five times the neighbouring
volume:

```
2020-02-14 (Twelve Data):  3 bars  — 14:00, 15:00, 16:00 only      7.9M
2020-02-18 (patched, HF):  7 bars  — full session, n_src 29/60×5/54  38.5M
2020-02-19 (Twelve Data):  2 bars  — 19:00, 20:00 only            13.3M
```

The patched day is not the anomaly. It is the only complete one. Its neighbours,
which no check had ever flagged, are missing four and five of their seven hours.
Monthly median hourly volumes agree on scale between the two sources (HF 3.7–5.0M
per hour, Twelve Data 7.5M in Feb 2020 rising to 30.6M in the March crash), so this
is coverage, not a volume convention.

**How wide.** Measured against the NYSE calendar over all twelve ETFs, 2002–2026:

| | |
|---|---|
| instrument-sessions | 67,424 |
| with at least one missing hour | 227 (0.337%) |
| bars expected | 470,258 |
| bars missing | 495 (0.105%) |

Not systemic. Concentrated where the whole-day damage already was — 2020 (39
sessions, 143 bars), 2021 (57, 177), 2007 (74, 101) — and zero in fourteen of the
twenty-five years. The largest single date is 2020-02-19: five hours gone from all
twelve instruments at once, four trading days before the COVID top.

**Why 0.1% is not small here.** An hourly return is taken between consecutive
*stored* bars. A missing hour does not shorten the series; it silently turns a
one-hour return into a two-hour one — a bigger move, still measured against the
one-hour scale the ladder ranks it on. The holes are rare and land precisely on the
quantity the severity tiers extrapolate from, at precisely the dates the system most
needs to have learned from.

**The fix.** `missing_hours` replaces the day with the hour as the unit: every
hour_utc the calendar claims inside the stored range, minus what the store holds.
Whole missing sessions fall out of it as the special case where every hour of a day
is absent, so the day-level view is now a report rather than a gate. `session_hours`
gives the calendar an hour-level reading, and it is deliberately conservative at both
ends — a 09:30–16:00 session is seven bars (09:00–15:00, since the 15:30 half-hourly
bar folds into 15:00), and an early close is asked for one bar *shorter* than the
store actually carries, because the 13:00:00 closing print lands in a bar of its own.
A bar that exists and was not demanded is not a hole; a bar demanded that cannot
exist would mark every ordinary day incomplete.

**What it recovered.** Run 34019217237, all twelve instruments:

> **gap fill: 333 hour(s) recovered, 365 confirmed missing at the source**

Counted against the calendar rather than against the store, the damage was 698 hours,
not 495 — the 29 whole sessions contribute 203 hours that the earlier table could not
count, because a day absent from the store has no row in it to be short. Twelve Data
recovered none of them again. HF Data recovered 333, and **every hour of 2020 and
2021 is now closed**: the years that held the COVID crash go from 320 missing bars to
zero.

**What is left is not missing.** The 162 hours that remain (the residue after the 203
belonging to whole sessions) sit entirely in 2003–2008, and 95 of them are HYG:

```
HYG launched 2007-04-11.
2007-04-11  13:00  100 shares    2007-04-12  11:00    700 shares
            14:00 1500                       12:00   2000
                                             14:00   1200
                                             15:00   2200
```

Its holes run 7 in April 2007, 17 in May, then 10, 8, 6, 4, 1, 3, 5, 5, 1, 1, and stop
at 2008-06-09 — the shape of liquidity arriving, not of a provider losing data. An
hour in which nobody trades produces no bar in *any* source, and two independent
vendors agreeing there is no bar, beside neighbouring hours carrying one or two
trades, is the strongest evidence available that there was nothing to record. The
same holds for the 2003–2006 singles in the bond and commodity funds. This residue is
irreducible and `missing_hours` will keep reporting it; that is correct behaviour for
a report and the reason it is not a gate.

**Why the 2020 half mattered more than its size.** §2.4 already decided what to do
with a hole: the return is taken from the last valid close, spanning two hours rather
than inventing a close that never existed. Honest about the price, but the two-hour
move is still ranked on the one-hour scale the ladder was fitted to. On 2020-02-19
that was five hours gone from **all twelve instruments at once** — which the new
market-wide channel (§26) reads as twelve instruments moving together, the exact
signature it exists to detect, four trading days before the top.

**The rule this is an instance of.** A completeness check inherits the resolution of
the unit it counts in, and that unit is a choice nobody remembers making. "The day is
present" was never the question — "the day is whole" was, and the two agree on every
case except the one that mattered. When a check reports zero, ask what it is
incapable of reporting.

## 29. A synthetic instrument, and why the anchor was doing nothing

The proposal was a standard one: take free hourly USD/CNH, anchor it to the free daily
onshore USD/CNY fix from FRED, and get an intraday mainland yuan "99.9% statistically
accurate" for nothing. It is a real technique. It does nothing here, and the reason is
worth writing down because it is a property of *this* system rather than of the method.

**The anchor cancels.** Anchoring multiplies every bar of a day by one constant,
`k_d = CNY_fix_d / CNH_close_d`. §2.4 scores the intra-hour channel, `r_t =
ln(close/open)`, and a constant inside a log ratio cancels exactly:

```
bars anchored: 39,955
max |r_synthetic − r_CNH| over every bar: 2.2e-16
identical to float precision: True
```

The anchor moves only the day boundary — which lands in the gap channel that §2.4
deliberately excludes from the Z-score, the CSV, PCA and SAED. **To the detector the
synthetic series is CNH, bit for bit, on every quantity it ranks.** What the FRED half
buys is not accuracy but the impression of holding onshore data.

**And the premise is false at this resolution.** Real CNY (our store) against real CNH
(Dukascopy), 32,300 shared hours:

| | CNY | CNH |
|---|---|---|
| intra-hour return correlation | **0.59** | |
| stdev | 5.65 bp | 6.42 bp |
| kurtosis | 54 | 14 |
| \|r\| at p99.99 | 92 bp | 73 bp |

Verified by the offset test — 0.59 at +0h and ≈0 at every offset from −3h to +3h — so
it is the pairs disagreeing, not the clocks. The level basis has median +5 bp but sd
21 bp and a range of −176 to +118 bp, and the widest days are 2020-01-31, 2022-11-04,
2020-05-01, 2022-05-02, 2025-01-31: Chinese holidays and reopenings, when the onshore
fix is stale and the offshore market is trading. On 5.3% of days the FRED fix does not
move at all. Those are the days a macro-event system exists for, and they are exactly
the days the anchor is a stale number.

**What was done instead.** USD/CNH is collected as itself, in `outside_basket` beside
USD/CNY. It takes no part in the quorum, the weights or breadth, so nothing already
calibrated moves; SAED still produces its alerts. Whether it should *replace* USD/CNY
in the FX block is a live question, and one that is easier to answer with both series
on disk than with neither — CNH covers ~100% of open hours from 2013 (84,838 traded
hours) against CNY's ~77% from 2019-11 (34,103).

**The rule this is an instance of.** Before evaluating whether a construction is
accurate, check whether the thing it constructs is read at all. Half of this one feeds
a channel the spec throws away, and no amount of accuracy in that half changes a single
score. "Is it right?" is the second question; "does anything downstream look at it?" is
the first.

## 30. Dukascopy, and four ways to build a wrong archive from a right file

Reaching for USD/CNH turned up a better source than the one already in use. FXCM starts
in 2012 and froze in April 2026; Dukascopy serves the same majors from **2003** — four
from May, three from August, probed rather than assumed — and carries USD/CNH from
2012-04. That closes a real hole: the ETFs reach 2002 and the pairs stopped at 2012, so
2008 was a crisis the basket could only half see.

There is no Node dependency and no key. The popular `dukascopy-node` package wraps the
same plain HTTPS files; one file is one month of one side, LZMA-alone over 24-byte
big-endian records. What follows is what the format does not tell you.

1. **The month in the URL is zero-indexed.** January is `00`. Off by one reads January
   as February and shifts an entire archive by a month while every file still parses.
2. **The record is open, CLOSE, low, high** — not OHLC. Reading it as OHLC swaps close
   and high and passes every sanity check that only asks whether high ≥ low.
3. **The integers are in units of the instrument's point**, which is 1e-3 for the yen
   pairs and 1e-5 for everything else. Measured: USD/JPY's June 2013 integers run
   94085..100672, which is 94.085 — one scale for all pairs puts the yen a thousand
   times low.
4. **A record exists for every hour of the month, including the closed ones** — volume
   0.0, and open, high, low and close all equal to the last traded price. EUR/USD's
   2013 Q1 returns 2,160 records for 1,516 traded hours. This is the dangerous one. The
   other 644 are not noise: they are exactly-zero returns, and writing them **deflates
   the volatility estimate the severity ladder is fitted to**. A ladder fitted to
   fabricated calm fires too easily. They are dropped on volume, and their volume is
   then discarded rather than stored — it is a tick-count proxy, not a consolidated
   exchange volume, which spot FX does not have anywhere.

Bid and ask are separate files and both are fetched, because the stored bars are mids.
Bid alone measures −0.19 bp against the store where the mid gives +0.00 — tiny, but
systematic and at a seam, which is the one place a small bias is not small.

**What it fetched.** Runs 34029405709 and 34035890999, +405,000 bars:

| pair | now reaches | pair | now reaches |
|---|---|---|---|
| EUR/USD, GBP/USD, USD/JPY, USD/CHF | 2003-05 | AUD/USD, NZD/USD, USD/CAD | 2003-08 |
| USD/CNH | 2012-06 | | |

USD/JPY's Dukascopy era spans 75.687..124.115 — 75.687 being the post-Fukushima record
yen high, which is the point size having been read correctly rather than asserted. The
FX store is now 1.85M bars against 1.43M, and the pairs sit in the same era as the ETFs.

**Which is the point.** Annualised hourly volatility by year:

```
        EUR/USD  GBP/USD  USD/CAD  AUD/USD  NZD/USD  USD/JPY  USD/CHF
2007        6.1      6.7      9.5     12.2     14.1      9.3      7.3
2008       14.3     15.3     16.3     26.6     23.3     16.4     14.7
2020        8.0     11.3      8.4     14.2     13.5      8.0      7.7
```

2008 is about twice the FX event 2020 was, and until this the basket had no FX data for
it at all. The severity ladder fits a GPD to the tail of each instrument's own
distribution; for the whole FX block that tail was being estimated from a sample that
excluded the largest event in the record.

**The splice is gated, unlike the last one.** FXCM began where the store already had
bars and had to be trusted blind. Dukascopy covers the whole stored range, so three
months of overlap can be bought for six extra requests a pair and the years underneath
made to earn their place. Measured end to end on live bytes, EUR/USD 2012 Q1 against
the store:

> 1,555 overlapping hours, correlation **0.9984**, median level gap **0.0786 bp**

Both §27 gates apply unchanged, nothing is written unless both pass, and nothing is
written at or above the oldest stored bar even then — Twelve Data stays the live source
and `bars.merge` lets the incoming row win. A test asserts that a thousandfold scale
error, which is precisely what fault 3 produces, is caught by the level gate rather
than merged.

**Operationally: a run may silently skip a pair, and that is by design.** The archive
rate-limits by IP on a short window — a burst earns HTTP 429, and it clears in about
twelve seconds, measured. The reader retries across a 45-second backoff, which covers
it in the ordinary case; when it does not, `fetch_history` raises and the per-asset
handler logs it and moves to the next pair rather than losing the ones already done.
That is what happened on the first pass: USD/JPY and USD/CHF were skipped under
throttling and a second run took them without incident. The right response to a pair
short of its floor is therefore to re-run it, not to investigate it — but the only way
to know is to check the floors afterwards rather than trust the run's exit status,
because a skipped pair is a successful run.

The other lesson is about where the commit sits. This job fetches for well over an
hour and commits once at the end, so a job killed at the timeout would lose everything
not yet written. It also collided with an ordinary branch push and lost a completed
USD/CNH pull outright, which is why the commit step now rebases and retries — it
earned that on the very next run, pushing on attempt 2.

## 31. The tier came from one bar and the magnitude from another

Re-running the whole chain on the deepened store surfaced a defect that had nothing to
do with the new data, and that had been in the events table since the tier ladder was
built.

**What it looked like.** SHY — the shortest-duration instrument in the basket — was top
of the push stream, and one of its pushes read:

```
tier      extreme          (a 3-year return period)
r         0.000056         (+0.0056%, a fifth of a basis point)
z_resid   2.10
retention_24  12.11        ("12x the original move a day later")
```

The delivery layer renders `r` beside the tier's own words, so that push would have gone
out as **"Treasuries 1–3 years — biggest move in 3 years, +0.01%"**. An alert that
discredits itself in its own first line.

**It was not the tier that was wrong.** `build_events` deliberately keeps the highest
tier an event reaches inside its cooldown, and the comment says why: a move that opens
routine and turns major an hour later is a major event, and reporting the opening tier
would understate it purely because of when the automaton opened. The hour-by-hour
residuals bear that out:

```
15:00  z_resid  2.10   routine     <- the event opens
17:00  z_resid 10.59   extreme     <- SHY +0.13%, a real rates move
```

The tier is correct. What was wrong is that only the tier moved. `z_resid`, `e_resid`,
`r` and `beta` stayed on the opening bar, so the row described two different hours as
one event — and `persistence.attach` then joined retention on the opening hour too,
dividing a genuine forward move by a fifth of a basis point to get 12x.

**How much of the output.** 763 of 12,551 events escalate inside their cooldown — 6.1%,
which sounds tolerable until you look at where they land: **171 of 510 pushes, 34%**.
A third of everything that would actually reach the phone stated a magnitude belonging
to a different hour than its headline.

**The fix.** The whole bar moves with the tier. Identity stays at the opening hour, so
`event_id`, the cooldown and idempotency are untouched; the description follows the bar
that earned the label, and a new `peak_hour_utc` records which bar that was. Retention
joins on it. The same SHY push now reads +0.13%, z_resid 10.59, retention 2.0x.

**The rule this is an instance of.** When one field of a record is updated conditionally,
ask which other fields were describing the same thing. A partial update leaves a row
that is internally inconsistent but individually plausible in every column, so nothing
downstream can detect it — the delivery layer had no way to know that its `r` and its
`tier` came from different hours.

## 32. It was never a Z-score, and the fix was not a floor

The first reading of this was wrong, and the wrong reading came with a proposed fix that
would have entrenched it. Recording both, because the mistake is the instructive part.

**What was seen.** Pushes firing on moves too small to be events: USD/CHF at **0.007%**,
IEF at 0.010%. Their `bmp_scale` — the peers' spread that `z_resid` is divided by — was
0.093 where the median is 0.94, turning a `z_resid` of 1.07 into a `z_resid_bmp` of
11.47. Hours with `bmp_scale < 0.25` are 0.7% of observations and were **6.8× over-
represented** in the push stream.

**The fix proposed, and why it was wrong.** A floor on the denominator, by analogy with
the `eps_MAD` floor the winsorization already applies. It would have worked in the sense
of removing the symptom. It was wrong for two reasons: the floor's value would have been
a number chosen by taste, and — the real objection — *the denominator was not the
problem*.

**What it actually is.** `z_resid / bmp_scale` is not a Z-score. The denominator is a
sample standard deviation of n peers, and the leave-one-out (§ the scale's own
docstring) makes it independent of the numerator, so

```
z / S  =  N(0,1) / sqrt(chi2_{n-1}/(n-1))  =  t_{n-1}, exactly
```

It is a **t-statistic**, and its heavy tail is not a defect but the correct sampling
distribution of a ratio whose denominator was estimated. Boehmer, Musumeci and Poulsen
(1991) use that cross-sectional spread as the denominator of a *portfolio* statistic,
`t = sqrt(N)·mean(SAR)/S(SAR)`, explicitly distributed `t_{N-1}`. Using it to rescale a
single instrument is an extension of the method, and the extension inherits the t.

**The defect is that n is not constant.** Between five instruments in session overnight
and twenty-three during the US afternoon, the degrees of freedom change every hour:

| peers n | share of pushes' hours | observed p99.9 | t(n−1) p99.9 | normal p99.9 |
|---|---|---|---|---|
| 5–8 | 40.9% | 11.77 | 6.87 | 3.29 |
| 9–12 | 31.1% | 8.26 | 4.78 | 3.29 |
| 13–17 | 5.3% | 5.80 | 4.32 | 3.29 |
| 18–24 | 22.7% | 5.84 | 3.85 | 3.29 |

The observed tail widens as n falls, in the order the t-distribution requires. So the
same number meant different things in different hours — a score of 11.5 is one hour in
eleven thousand against six peers and one in eleven **billion** against twenty-three —
and both were handed to one severity ladder, which read the thin overnight hours as the
violent ones. Hours with a small scale are the same hours: median six peers against
eleven overall, 96% of them with n ≤ 10.

**The transform.** Wallace's (1959) normalising approximation for Student's t, which
maps a t onto the standard normal scale given its own degrees of freedom:

```
z* = sign(t) · sqrt(v · ln(1 + t²/v)) · (8v + 3) / (8v + 1)
```

Chosen over the exact probability integral transform after measuring both on the store:
the exact transform leaves the widest and narrowest peer-count buckets differing by
1.09×, Wallace by **1.08×** — no better, because what remains is the residuals not being
exactly normal rather than any inaccuracy in the mapping. Wallace needs a logarithm and
a square root; the exact transform needs an incomplete beta function, and with it scipy,
which this project does not carry and which would not earn its place for one transform.
Cornish–Fisher was tried and rejected: it diverges in the tail, reaching an error of 652
where Wallace's worst is 0.5.

**What it bought, measured.**

| | before | after |
|---|---|---|
| widest ÷ narrowest peer-bucket tail | 2.03× | **1.08×** |
| noisy-denominator hours' share of pushes | 6.8× over-represented | **3.8×** |
| the 0.007% USD/CHF push | present | gone |

**What it did not fix, stated plainly.** IEF at 0.010% and SHY at 0.017% still push.
That is a different question and not a statistical one: the two shortest-duration bond
funds have genuinely tiny distributions, so a small move really is rare *for them*. The
system is built to rank each instrument against itself, and nothing in it says a move
must also be large in absolute terms to be worth a notification. Whether it should is a
question about the recipient, not about the estimator, and it is left open rather than
answered with another floor.

**The rule this is an instance of.** When a statistic misbehaves, ask what its sampling
distribution actually is before reaching for a guard. The guard treats the symptom in
the hours where it shows and leaves the calibration wrong everywhere else — and it hides
the fact that the quantity was never on the scale the code assumed. `cross_sectional_scale`
and `standardise_cross_section` had no direct tests at all until this; the function at
the centre of the detector's cross-sectional standardisation was the one nothing checked.

## 33. A one-cent move is not a small event, it is an unobserved one

§32 closed with IEF pushing on 0.010% and the claim that this was a question about the
recipient rather than the estimator — that a small move can be genuinely rare for a
quiet instrument, and nothing in the system says an event must also be large. That was
half right and it stopped one question too early. The right question was not "is 0.010%
big enough to matter" but **"is 0.010% a move at all"**.

**It is one tick.** IEF trades near $98 with a one-cent tick, so a cent is 0.0102%. The
pushed move was 0.0101% — **0.99 ticks**, the smallest non-zero change the instrument
can express. Every other instrument's smallest pushed move ran from 10 to 60 ticks:

```
IEF   "extreme"  -0.0101%  =  0.99 ticks
SHY   "extreme"  +0.0174%  =  1.45 ticks
SPY   smallest pushed move =  12.9 ticks      GLD  59.9   USO  49.1
```

**SHY is a large-tick asset**, in the microstructure sense the literature gives the term:
the price resists moves of a single tick and the spread sits at one. Hourly price changes
in ticks, over the whole store:

| ETF | median move | hours that do not move at all | within one tick |
|---|---|---|---|
| SHY | **1.1 ticks** | **29.8%** | 64.1% |
| IEF | 5.4 | 7.0% | 18.2% |
| SPY | 29.9 | 1.4% | 3.9% |

Nearly a third of SHY's hours have no price change whatsoever. Its return distribution is
a measurement of the price grid, so a ladder fitted to it ranks the grid — which is
exactly how one cent came to be reported as the biggest move in three years.

**Why this is not the floor rejected in §32.** That one would have clipped a statistic
whose distribution had been misread, at a value chosen by taste. This one encodes a
physical property of the exchange. And the threshold is derived rather than picked: if
prices are recorded on a grid of one tick, an observed change of one tick is consistent
with a true change of almost nothing, while **two ticks is the smallest observed change
that guarantees the true move exceeded one tick**. Hence two, and not a number chosen for
how it looked.

The same reasoning is already in the code one layer down. §2.5's winsorization floors
`eps_MAD` at the return on half a tick, precisely so that "in quiet hours when the price
stands still, MAD collapses to zero and an ordinary move looks extreme". The scale knew
about the tick; the event did not. This extends it.

**It is not the second magnitude filter §8.3 refuses.** That refusal — "a single-asset
move is grounds in itself, and how much it matters is carried by the tier" — presumes the
move was observed. Below two ticks what varied was the rounding. This is a gate on
observability, not on significance, and the two are worth keeping apart: the first is a
fact about the instrument, the second would be a judgement about the recipient.

**What it cost.** 1.2% of events and 3 of 653 pushes, landing where it should:

```
SHY  -15.9% of its events        IEF  -2.9%       everything else  <= 2%
smallest pushed move, before:  0.99 ticks    after:  2.05 ticks
```

**What remains, and is genuinely the open question.** SHY still pushes at 2.05 ticks, or
0.025%. That move is now certainly real — but whether a quarter of a basis point on a
one-to-three-year Treasury fund should interrupt anyone is the question §32 wrongly
claimed this one was. It is a choice about the recipient, and it is still open.

**The rule this is an instance of.** "Rare for this instrument" and "observable on this
instrument" are different properties, and a percentile cannot tell them apart: the grid
produces a perfectly good distribution with perfectly good tails. Before accepting that a
small number is rare, check what the smallest number the instrument can produce actually
is.
