# Working on Jump

An hourly Telegram bot on GitHub Actions that writes when one of 62 market instruments
moves unusually for itself.

**Where things are**
- `README.md` - what it is, and a map of the files
- `docs/manual.md` - how each part works; read the section before changing that part
- `docs/decisions.md` - why things are the way they are, and what was tried

**Firm**
- The channel carries alerts only; health and provider failures go to
  `TELEGRAM_HEALTH_CHAT_ID`.
- The repository is public: keys live in GitHub Actions secrets, never in files.
- The live branch (`main`) runs the bot: a push there reaches the channel within the hour.
  Develop on `dev`. Ask before pushing to `main`, unmuting, deleting data or messages, or
  rewriting history.

**Recommended**
- Keep a change to what was asked; mention other problems instead of folding them in.
- Before changing what gets flagged or how messages look, show the numbers and ask.
- Update the manual (and the record, if a choice was made) with a behaviour change.
- Tiingo, SiftingIO and Twelve Data quotas belong to the live run; test with free sources.
- A bad bar goes in `data/jump/verified.csv`; bars change only through the fetch.
- Give numbers with their window, per instrument or in σ.
- `pytest -q` (about 905 tests, 2 minutes), run alone. A regression test should fail
  without the fix.
- Answer plainly and briefly; recommend rather than survey.
