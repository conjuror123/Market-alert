# How an agent works here

Tremor is an hourly Telegram bot on GitHub Actions that writes when one of 173 market
instruments moves unusually for itself. `README.md` says what it is and maps the files;
`docs/manual.md` says how every part works; `docs/decisions.md` says why, and what not to
reopen.

**Before changing code, read the section of `docs/manual.md` that covers it.** Its
**Rule.** lines are invariants: a change that breaks one makes the system wrong, not
merely broken. A change to behaviour updates the manual (what it does) and, where a choice
was made, `docs/decisions.md` (why), in the same commit.

## Boundaries

- **The channel is private**, and carries the alerts only. Health and provider failures go
  to `TELEGRAM_HEALTH_CHAT_ID` only, never to the channel.
- **Secrets never enter the repository.** It is public. Keys live in GitHub Actions
  secrets and are read from the environment; `config/config.yaml` may name a secret,
  never hold one.
- **Push only to the branch you were asked to**, and open a pull request only when asked.
  The live branch runs the bot: a push there reaches the channel within the hour.
- **Confirm before anything outward-facing or hard to reverse:** a push to the live
  branch, unmuting, deleting data or channel messages, rewriting history. One approval
  does not cover the next.
- **Do not spend a provider's quota on experiments.** Tiingo and SiftingIO run near their
  limits for the live run; probe free sources instead.

## Scope

- **Do what was asked, then stop.** Fixing a thing is not a request to refactor its file.
- **Finish it.** If part is blocked, do the rest and say what was left and why.
- **A second problem found on the way is reported, not silently fixed.** Two changes in
  one commit cannot be reviewed or reverted apart.
- **Stop for a go before a change to what the detector flags or what the channel
  shows.** Present the measured before/after first.

## Evidence

- **Measure the thing you are about to claim**, not something adjacent. A plausible
  mechanism is not evidence.
- **Quote a number with its window** ("4 pushes a week, over the year to 2026-10-01").
  Re-derive rather than copying a figure forward across a change.
- **Never a percentage across instruments.** 5% is a quiet hour for a coin and a crash
  for short Treasuries. Per instrument, in σ, or nothing.
- **Never present a partial run as complete.** A killed job, a rate limit or a skipped
  stage is said in the same breath as the result.
- **When told you are wrong, check, then answer.** Neither fold nor dig in. When wrong,
  say so in one sentence.

## Changing it safely

- **Run the whole hourly pass, not one stage** (`docs/manual.md`, "The hourly pass").
  A stage alone leaves the next reading stale data.
- **A formula change moves `config_version` and forces a cold rebuild.** Run it, and
  check the diff in events is what you expected.
- **No window may see past the bar it judges.** The easiest rule to break by accident and
  the hardest to notice.
- **A regression test must fail without the fix.** One that passes either way looks like
  cover and is not.
- **A rewrite claiming to be exact is checked against the old code on real data.**
- **Run the tests alone:** `pytest -q`, about 700 tests in 2 minutes. Several load large
  parquet files, and concurrent runs thrash.

## What cannot be self-reported

State these plainly rather than guess: effort or reasoning depth, billing and usage, and
anything not verified in this session — including earlier numbers, if the code has
changed since.
