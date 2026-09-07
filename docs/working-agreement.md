# Working agreement

Rules for an agent changing this repository. Adapted from a general checklist for
financial applications; the parts that did not fit are marked and the reasons given,
because a rule nobody believes is a rule nobody follows.

The evidence for most of these is in `docs/meals-deviations.md`, where each numbered
section is a mistake or a decision with the measurement that settled it.

---

## 0. The one rule the others are instances of

**Measure before deciding, and measure the thing you are about to claim.**

Nearly every section of the deviations log exists because something was assumed that
took four commands to check. A description is a claim. A docstring is a claim. A prior
decision is a claim. The measurement is the evidence, and it usually costs less than the
argument about it would have.

---

## 1. Boundaries

- **Know what is uncommitted before starting, and why.** Not "never start with a dirty
  tree" — a pipeline run legitimately leaves regenerated Parquet behind, and this repo's
  own stop hook requires committing and pushing. The failure to avoid is *not knowing*
  what changed, not the changing.
- **Edit only what the task needs.** If a fix requires touching a file the request did
  not mention, say so in the reply rather than folding it in silently.
- **One branch.** Do not spin up alternative implementations to compare unless asked.
- **Push what you promise.** A run that fetched data and failed to push it has lost the
  data with the container. This has happened here (§34's commit note); the commit step
  now rebases and retries.

## 2. Persistence, and its limit

The general checklist said: *if two search rounds return no actionable results, stop.*
**Rejected as written.** The most valuable findings in this repository came from a third
and fourth look:

- a volume "anomaly" on a patched day turned out to be the only *complete* day, which
  revealed that whole-day gap detection had never seen missing hours (§28);
- a score of 11.5 that looked like a bad denominator turned out to be a t-statistic whose
  degrees of freedom nobody had tracked (§32);
- a 0.010% move that looked like a small event turned out to be one tick (§33).

The real rule is not a round limit, it is a **quality bar per round**:

> Each round must end in a measurement or a decision. A round that ends in a new hunch is
> the signal to stop and report what is known.

Stop also when the next step would change behaviour the user has an opinion about — that
is a question, not a search.

## 3. Scope

- **Do not widen the change.** Fix what was asked.
- **Do widen the looking.** Reporting a defect you found while nearby costs a paragraph;
  shipping around it costs the user's trust in every alert afterwards. The push that read
  *"biggest move in 3 years, +0.01%"* was found this way, and it was 34% of the push
  stream.
- The distinction: **edits stay narrow, observation stays wide, and everything observed
  gets said.**

## 4. When the user says you are wrong

**Re-derive it. Do not defend it.** Trace the logic fresh, with data if data exists.

This is not politeness, it is the empirically better bet in this repository. Scored:

| the user said | outcome |
|---|---|
| "every instrument has the same rate — ultra-suspicious" | correct; drove the absolute channel |
| "0.010% is extremely low, don't assume it's rare" | correct; it was one tick |
| "alerts/year is skewing the average feeling" | correct; median gap was 7 days, not 12 |
| "maybe there is a more clever way than a floor" | correct; it was a t-distribution |

And the converse, which matters as much: **when the premise is wrong, say so with a
measurement, not with deference.** Investing.com, FRED and the Kaggle calendar were all
proposed here and all declined — each with numbers, and one of them (Kaggle) only after
downloading it, because the proposal differed from the one previously rejected and
deserved its own test (§35–37).

## 5. Money and numbers — the general advice does not apply here

The source checklist said: *avoid native floats for currency, use BigInt or Decimal,
keep integer cents.* **Wrong for this repository, and following it would do damage.**

That advice is for **ledgers** — balances, settlement, invoices — where a half-cent is a
defect and sums must reconcile exactly. This application holds no balances and settles
nothing. Its numeric core is log returns, EWMA variance, a generalised Pareto tail fit,
eigendecomposition of a correlation matrix. Those are float64 operations; `Decimal` would
be slower, would not improve accuracy, and cannot express the operations at all.

What the section was groping toward is real, and this repository has met it:

- **Price discreteness.** A move smaller than the instrument's tick is not a small move,
  it is an unobserved one. SHY does not move at all in 29.8% of hours and its median
  hourly change is 1.1 ticks; a return ladder fitted to it ranks the quantisation grid.
  `quality.resolvable` gates on two ticks — the smallest observed change that guarantees
  the true move exceeded one (§33).
- **`tick_size` is configuration, not a measurement of the vendor's float format.** The
  source serves `769.53992` where the exchange trades in cents.
- **Scale factors differ by instrument and are not documented.** Dukascopy stores prices
  as integers in units of the instrument's point: 1e-3 for yen pairs, 1e-5 for the rest.
  One scale for all puts USD/JPY at 0.94 (§30).
- **A denominator estimated from data has a sampling distribution.** Dividing by it does
  not produce a z-score. Know which distribution you are on before ranking anything by it
  (§32).

## 6. Time

The one part of the original checklist that needed strengthening rather than fixing.

- **Everything is UTC in storage.** Local time exists only where a human reads it or an
  exchange defines a session.
- **Never assume a vendor's timezone — test it.** Fit every offset from −6 to +6 hours
  against data already held and let the winner declare itself. Zero has won on FXCM and
  Dukascopy; HistData is US/Eastern *with* daylight saving and says so nowhere, and
  reading it as UTC drops return correlation from 0.94 to 0.53.
- **A session is not a number of hours.** Half sessions, DST, and the difference between
  an exchange calendar and a 24/5 week all bite. Compute session hours from the calendar
  (`sessions.session_hours`), never by arithmetic on a timestamp.
- **Sub-daily gaps hide inside present days.** A completeness check inherits the
  resolution of the unit it counts in (§28).

## 7. Changes that reach the user

- **Never state a claim the system did not test.** An alert saying the economic calendar
  failed to explain a move, when nothing consulted the calendar, is false however well it
  reads. A test now asserts no alert text contains "calendar" (§36).
- **A mean is the wrong summary of a bursty process.** Quote the median gap and the
  quantiles; "one every twelve days" described no month in twenty-five years (§34).
- **Guard values must be derived, not chosen.** Two ticks follows from the rounding
  interval. A floor on `bmp_scale` would have been a number picked by taste, which is why
  it was rejected in favour of understanding the distribution (§32, §33).

## 8. Consistency across the change

If a computation changes, find everything that reads it: stored schemas, the columns
persisted to Parquet, the delivery layer, the tests, and any document that quotes its
numbers. Adding a column to a residual frame and forgetting `RESIDUAL_COLUMNS` means the
value exists in memory and not on disk, and the next reader gets a `KeyError` from a
different module.

## 9. What I cannot self-report

Stated plainly because the original asked for it and I will not fake it:

- **Effort.** `CLAUDE_EFFORT` is readable from the environment (currently `xhigh`), but
  that is the setting the harness was given, not evidence about what was delivered. I
  cannot verify my own reasoning depth, and a claim that I am "running at xhigh" means
  only that I read a variable.
- **Billing and usage.** No visibility. If subagents fail or stop, I can report *that*,
  and will; I cannot see cost.
- **Issue numbers.** I cannot verify the tracker IDs quoted in the source checklist and
  have not repeated them as fact.

What I can do instead: report tool failures, killed background jobs, rate limits and
truncated runs when they happen, and never present a partial run as a complete one.
