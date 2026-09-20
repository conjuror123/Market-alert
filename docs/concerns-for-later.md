# Concerns for later

> **Holds** what is known, measured where possible, and deliberately not being worked on.
> **Does not hold** settled questions (`decisions.md`) or operational limits that are
> simply how it works (`operations.md`).
> **Add an entry when** something is found and left alone. Say what it is, what it costs,
> and what acting on it would mean — so that leaving it stays a decision rather than an
> oversight.

Nothing here is a bug in the sense of producing a wrong message today.

**Two standing limits that will not change.** It does not predict, and does not claim to:
every number is about a move that already happened, and the tier says how unusual it was,
not what comes next. And the ladder cannot claim a return period longer than an
instrument's own history — a newly added name says "biggest in a quarter" for years before
it can say "biggest in six", and nothing at all for the first two.

---

## 1. Thirteen instruments are walled at 2020-02-10

Verified on the store: thirteen instruments hold no bar before 2020-02-10, and XLP holds
none before 2022-08-01. Everything else reaches 2002–2007, and the currency pairs reach
2003.

The wall is a provider plan limit, not a bug. The consequence is that those instruments
are **quieter than the rest by design**: an instrument with six years of history cannot
say "the biggest since 2008". **Five instruments cannot reach the top rung at all today**
for this reason.

XLP at 2022-08-01 is separate and unexplained — 7,246 bars against XLK's 11,572, from the
same provider on the same plan. Nothing in the repository accounts for the difference.

**What acting on it would mean:** deepening history from an archive provider, which is a
data migration rather than a code change, plus finding out what is different about XLP.

---

## 2. Repository size

565 MiB packed, 590 MiB on disk. GitHub starts warning at 1 GB.

The committed bars are only 84 MiB of that; the rest is history — every rewrite of every
parquet since the store began, which git cannot delta because parquet is compressed.
Sharding the live year by month (already done) slowed the growth; it did not undo what is
already in the history.

**What acting on it would mean:** rewriting history to drop superseded parquet blobs,
which force-pushes the branch production runs from and is irreversible. That is the
reason it has not been done, not the effort.

---

## 3. The feed comparison rests on 62 hours

**What the feed split is.** The basket is not served by one data provider. Each
instrument is assigned to the feed that was shown to price *it* correctly:

| provider | instruments | what it is |
|---|---|---|
| `tiingo` | 37 | IEX — a single exchange |
| `yahoo` | 15 | a consolidated feed — an undocumented endpoint with no SLA, which can change shape without notice. That is why the funds are split across two providers rather than sent to one |
| `coinbase` | 9 | the exchange itself, for crypto |

**Why the assignment needed measuring at all.** IEX is one exchange holding roughly 5.5%
of US equity volume; a consolidated feed sees the whole tape. Two feeds can both be
complete and still disagree about where a thin ETF closed at 14:00, because they saw
different prints. On a liquid fund that difference is a fraction of a basis point. On a
thin single-commodity fund it is not — and a few basis points of disagreement, against an
hourly sigma of 20–40 bps, is not noise. **It is an alert for a move that never
happened**, arriving through the data rather than through the arithmetic. That is the one
failure worth paying to avoid, and it is why fifteen thin funds stayed on the
consolidated feed instead of moving with the rest.

**What was actually measured.** `tools/tiingo_compare.py` fetched the same hours from
Tiingo, folded them to the hourly grid with the same code the pipeline uses, joined them
to the bars already stored, and reported the median absolute difference in basis points,
per instrument. Instruments that agreed closely moved; instruments that did not, stayed.

**The concern.** That comparison covered **62 overlapping hours — one week**, and has not
been repeated since. One week is one volatility regime. A feed that tracks the
consolidated tape closely in a quiet week can diverge in a violent one, precisely when an
alert matters most, because that is when prints scatter across venues. Several
instruments sat close to the line where the decision flips; their assignment rests on a
sample too small to separate them confidently.

Nothing is known to be wrong. The point is that the evidence is thin, and the assignment
it produced is treated as settled.

**What acting on it would mean:** re-running `tools/tiingo_compare.py` — 44 requests,
read-only, runs in Actions where both the key and the store are — over a longer and
ideally more volatile window, and recording the sample size as a limit next to the
result. Cheap. It is on this list rather than the other one only because nothing is
visibly broken.

---

## 4. Better and deeper data

The general version of concerns 1 and 3: more history, better-verified providers, and
more instruments. Grouped here because they are one programme rather than three tasks,
and because each of them is a data migration with a verification step rather than a code
change.

---

## 5. The rungs have not been re-seeded since the sigma estimator changed

`sigma_LT` became an exponentially weighted estimate with a per-calendar half-life
(`docs/decisions.md`). The ladder in `tremor/severity.py` is written in multiples of
`sigma_LT`, and those multiples were seeded against the flat 5,000-bar box that estimator
replaced.

Measured consequence: the alert rate fell about 10% across the basket, and backtest
recall went from 97.2% to 94.4% of the most obvious hours.

This was deliberate — changing the estimator and the ladder in the same step would have
left neither measurable — but it is a deferral, not a decision. The rungs are a
preference the reader owns, so re-seeding them is a question for the reader, not a fix.

**Related, and in the same area:** events found by **both** routes at once — larger than
the instrument's own history explains *and* larger than its sector accounts for — hold up
markedly better than either route alone (80.2% still standing at the next close, against
72.6% abnormal and 63.6% absolute). `PUSH_TIERS` keys on tier alone, so a `major` found
both ways and a `major` found one way are treated identically. There is a real quality
gain available here at no extra volume, and it has not been taken because it changes what
reaches the phone.

---

## 6. Comments and prose that have drifted

Small, cosmetic, and worth a pass rather than a project. Nothing specific is currently
listed here — the prose drift that was on this list turned out to be one substantive
error rather than a cosmetic one, and is closed in `docs/decisions.md`.

The standing rule is the useful part: **a comment that describes a mechanism is a claim,
and claims go stale silently.** Prose that merely reads awkwardly can wait. Prose that
asserts how something works cannot, because the next reader will believe it.
