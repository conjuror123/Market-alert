# Working agreement

> **Holds** the rules an agent works under here: boundaries, scope, what cannot be
> self-reported.
> **Does not hold** anything about the system itself — that is `CLAUDE.md` and the
> documents it maps.
> **Add a rule when** a way of working has gone wrong and the fix is a habit rather than
> a code change.

Rules for anyone — person or agent — changing this repository. `CLAUDE.md` is the shorter
orientation; this is what you work under.

---

## The rule the others are instances of

**Measure before deciding, and measure the thing you are about to claim** — not the thing
that is easy to measure and adjacent to it.

A plausible mechanism is not evidence. "Half the runtime is the rolling MAD" and "Parquet
will not delta-compress in git" were both sound reasoning and both wrong by an order of
magnitude; each took ten minutes to test.

---

## Boundaries

**Secrets never enter the repository.** `TIINGO_API_KEY`, `TWELVEDATA_API_KEY`,
`FRED_API_KEY`, `HFDATA_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`TELEGRAM_HEALTH_CHAT_ID` live in GitHub Actions secrets and are read from the
environment. The repository is public. `config.yaml` may name a secret; it may never hold
one.

**Do not push to a branch you were not asked to push to**, and do not open a pull request
unless asked.

**Confirm before anything outward-facing or hard to reverse.** Unmuting alerts, pushing to
the branch production runs from, deleting data. Approval for one is not approval for the
next.

---

## Scope

**Do what was asked, then stop.** A request to fix the thing is not a request to refactor
the file it lives in.

**But finish it.** If part of the work is blocked, do the rest and say plainly what was
left and why. Scaling the job down is the user's call.

**If you find a second problem while fixing the first, say so — do not silently fix it.**
Two changes in one commit make both harder to review and impossible to revert separately.

---

## When the user says you are wrong

**Check, then answer.** Do not fold because they pushed, and do not dig in because you
already committed to it. Both are ways of not doing the work. A flat "that can't be right"
is worth a real check — several of the better findings here started as one.

When you are wrong, correct it in one plain sentence and move on. No ceremony.

---

## Numbers

**Quote the measurement and its window.** "56 pushes a year over 23 years of hourly bars"
is a number; "not many alerts" is not. Re-derive rather than copying a figure forward — a
number that was true before a change to the ladder is not evidence about the system now.

**A percentage across instruments is almost always meaningless here.** 5% is a quiet hour
in SOL and an apocalypse in SHY. Per-instrument or nothing — this is the premise of the
whole system, and it is easy to forget when writing a summary.

**Never present a partial run as a complete one.** If a job was killed, a rate limit was
hit or a stage was skipped, say so in the same breath as the result.

---

## Time

**Everything internal is UTC, in seconds, named `hour_utc`.** Local time appears in
exactly one place — the digest slot, because the reader reads it locally — and is resolved
through `ZoneInfo` so it tracks daylight saving.

**Never let a window see past the bar it judges** (invariant 2). A full-sample fit labels
a 2016 move knowing 2020 is coming, and the backtest then flatters a system nobody can
run. This is the easiest rule to break by accident and the hardest to notice afterwards.

---

## Consistency across a change

The invariants themselves are in `CLAUDE.md` and are not repeated here. What belongs here
is what to *do* about them.

**Run the whole sequence, not one stage** (invariant: the order is load-bearing). Running
a stage alone can leave the next one reading yesterday.

**After a formula change, expect a cold rebuild and check it** (invariant 10). The first
run is slow by design; re-run the sequence before committing and confirm the diff is what
you expected rather than assuming it.

**Tests are the guard on anything that must be identical.** A rewrite claiming to be exact
should be checked against the implementation it replaces, on real data, not asserted in a
commit message. A regression test must be shown to fail without the fix — one that passes
either way is worse than none, because it looks like cover.

---

## What cannot be self-reported

State these plainly rather than faking them:

- **Effort or reasoning depth.** A setting read from the environment is not evidence about
  what was delivered.
- **Billing and usage.** No visibility. Tool failures, killed jobs and rate limits can be
  reported, and should be; cost cannot.
- **Anything not verified in this session** — including numbers from earlier in the same
  conversation, if the code has changed since.
