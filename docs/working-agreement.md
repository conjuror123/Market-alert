# Working agreement

Rules for anyone — person or agent — changing this repository. Every one of them was
learned by getting it wrong at least once; the evidence is in `decisions.md`.

---

## The one rule the others are instances of

**Measure before deciding, and measure the thing you are about to claim.**

Not the thing that is easy to measure and adjacent to it. Three examples from this
repository, all of which read as sound reasoning until measured:

- A quorum on peer count looked like the obvious fix for a thin cross-section. Measured,
  fewer than ten peers is 37.5% of all hours, and those hours contain the SNB unpegging,
  Brexit and the yuan devaluation.
- "Half the pipeline is the rolling MAD" was a guess worth 54% of the runtime — but the
  reason was call overhead on a 24-element window, not the arithmetic. The fix was
  vectorisation, not caching, and it was 17× rather than the 2× a caching design would
  have bought.
- Parquet was assumed not to delta-compress in git, implying 4.9 GB a day. Tested in a
  throwaway repository: 1 MB a day.

---

## Boundaries

**Secrets never enter the repository.** `TWELVEDATA_API_KEY`, `FRED_API_KEY`,
`HFDATA_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` live in GitHub Actions secrets
and are read from the environment. The repository is public. `config.yaml` may name a
secret; it may never hold one.

**Do not push to a branch you were not asked to push to**, and do not open a pull request
unless asked.

**Confirm before anything outward-facing or hard to reverse.** Unmuting alerts, pushing
to the branch production runs, deleting data. Approval for one of these is not approval
for the next.

---

## Scope

**Do what was asked, then stop.** A request to fix the thing is not a request to
refactor the file it lives in.

**But finish it.** If part of the work is blocked, do the rest and say plainly what was
left and why. Scaling the job down is the user's call.

**If you find a second problem while fixing the first, say so — do not silently fix
it.** Two changes in one commit make both harder to review and impossible to revert
separately.

---

## When the user says you are wrong

**Check, then answer.** Do not fold because they pushed, and do not dig in because you
already committed to it. Both are ways of not doing the work.

Several of the better findings here came from a flat "that can't be right":

- "Why put a hard floor in it, maybe there is a cleverer way?" — there was. The
  denominator was making a t, not a z, and the answer was a normalising transform.
- "0.010% is extremely low, don't you think?" — it was. The instrument had moved one
  tick and the ladder, fitted on a distribution made of one-tick jitter, called it
  historic.
- "We are measuring predictive ability when the algorithm is post-factum analysis" —
  correct, and it exposed real look-ahead in the scoring harness.

And when you are wrong, correct it in one plain sentence and move on. No ceremony.

---

## Numbers

**Quote the measurement and its window.** "22 pushes a year" is a number; "not many
alerts" is not.

**A percentage across instruments is almost always meaningless here.** 5% is a quiet
hour in SOL and an apocalypse in SHY. Per-instrument or nothing — this is the whole
premise of the system, and it is easy to forget when writing a summary.

**Never present a partial run as a complete one.** If a background job was killed, a
rate limit was hit, or a stage was skipped, say so in the same breath as the result.

---

## Time

**Everything internal is UTC, in seconds, named `hour_utc`.** Local time appears in
exactly one place — the digest slot, because the reader reads it in local time — and is
resolved through `ZoneInfo` so it tracks daylight saving.

**Rolling windows end before the bar being judged.** Every one of them. A full-sample fit
labels a 2016 move with the knowledge that 2020 was coming, and the backtest then
flatters a system nobody can run.

---

## Consistency across a change

**The pipeline order is load-bearing.** `cross_section` reads what `pipeline` wrote,
`saed` reads the basket factor, and `cluster` writes eighteen columns *into*
`metrics_basket_hour.parquet` — so running a stage alone can leave a file stripped rather
than merely stale. That trap has been hit twice.

**A change to the maths invalidates the committed derived data.** Re-run the whole
sequence before committing, and check the diff is what you expect: if only
`config_version` and `run_version` moved, the change was exact.

**Tests are the guard on anything that must be identical.** A rewrite claiming to be
exact should be checked against the implementation it replaces, on real data, not
asserted in a commit message.

---

## What cannot be self-reported

Stated plainly rather than faked:

- **Effort or reasoning depth.** A setting read from the environment is not evidence
  about what was delivered.
- **Billing and usage.** No visibility. Tool failures, killed jobs and rate limits can
  be reported, and should be; cost cannot.
- **Anything not verified in this session.** Including numbers from earlier in the same
  conversation, if the code has changed since.
