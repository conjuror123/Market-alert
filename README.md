# Market Alert

Once an hour the bot checks the prices of a chosen set of assets — crypto, gold,
oil, indices, currencies — and writes to Telegram if a price move looks genuinely
unusual.

What counts as "unusual" is not decided by a fixed daily percentage. The algorithm
looks at how this particular asset has been behaving lately, and compares the
current move against exactly that.

It runs entirely on GitHub Actions. Nothing extra needs hosting.

## Quick start

1. Create a Telegram bot through [@BotFather](https://t.me/BotFather) and get a token.
2. Decide where the bot should send its notifications. There are two options:
   - **To yourself.** Send the bot `/start`, then find your `chat_id` — for example
     by opening `https://api.telegram.org/bot<TOKEN>/getUpdates` after you have
     written to it.
   - **To a channel**, if you want other people to be able to subscribe. Create a
     public channel and add the bot as an administrator with the right to post.
     In that case `chat_id` is simply the channel's `@username`, with no numeric ID
     to hunt for.
3. Open the repository settings: **Settings → Secrets and variables → Actions →
   Secrets**. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` there.
4. Currency pairs go through a separate provider, [Twelve Data](https://twelvedata.com/) —
   register there (email only, no card) and add the key alongside the others under the
   name `TWELVEDATA_API_KEY`. Without it the hourly monitoring keeps working on the
   remaining assets, but stays silent on all eight currency pairs.
5. Set up the hourly trigger — this is mandatory, otherwise the bot will never start
   on its own at all, only manually through Run workflow. GitHub Actions' native
   schedule is unreliable on repositories like this one (it can genuinely fire once
   every 5-10 hours), so instead use an external free alarm clock, see "A reliable
   hourly trigger" below (~10 minutes of setup).

To check the setup without waiting for a real signal: **Actions → Test Telegram
Notification → Run workflow** — it sends a test message and nothing else.

## Which assets are tracked

Sixteen by default: Bitcoin, Ethereum, Solana, gold, WTI crude, the S&P 500,
30-year US Treasuries, the dollar index (DXY) and eight currency pairs — EUR/USD,
GBP/USD, USD/JPY, USD/CHF, USD/CAD, AUD/USD, NZD/USD, USD/CNY.

The asset list lives in `config/config.yaml`. To add or remove an asset it is enough
to edit that file — no code changes needed.

<details>
<summary><b>How to add a new asset</b></summary>

1. Find the ticker and check it for real rather than from memory — not every ticker
   that looks plausible actually exists. For the dollar index, for instance, `DX=F`
   did not work (404, no such ticker) while `DX-Y.NYB` did. A quick check:
   ```bash
   python3 -c "
   from price_monitor import yahoo   # or price_monitor.coinbase / twelvedata
   import requests
   candles = yahoo.fetch_klines('TICKER', '1h', limit=20,
       base_url='https://query1.finance.yahoo.com', session=requests.Session())
   print(len(candles), candles[-1].close, candles[-1].volume)
   "
   ```
2. Add an entry under `assets:` in `config/config.yaml` — `symbol`, `source`,
   `label`, and `news_query` if `label` does not work as a search query.
3. Run the backtest and look at the new asset in the report:
   ```bash
   python -m price_monitor.backtest --days 365 --out data/backtest_results.json
   ```
   Look at three things: notifications per week, recall on the top-5 largest moves,
   and whether the numbers look structurally broken (as they did for the S&P 500 with
   volume, or for USD/CNY with stuck quotes — see the backtest section above).
4. If something is structurally wrong with that particular asset, add a targeted
   override right in its entry in `config.yaml` (see "Per-asset thresholds" below)
   rather than bending the shared thresholds around a single asset.
5. Bear in mind that the shared thresholds have a target total frequency across all
   assets together (see the backtest section above) — a new asset lifts it slightly.
   Once is harmless; if the asset count grows a lot, it may be worth recomputing the
   shared thresholds — your call.

**A note on currency pairs specifically:** `source: twelvedata` runs into the free
tier's limit of 8 requests per minute (one per pair), see "Data-source quirks" above.
Adding a ninth pair without removing one of the current eight is impossible without
moving to a paid Twelve Data plan. The Twelve Data symbol is `BASE/QUOTE` with a
slash (`EUR/USD`, not `EURUSD=X` as it was on Yahoo).

</details>

## How the bot decides what to write

Every asset has its own normal volatility: what is an earthquake for bonds is an
ordinary day for a cryptocurrency. So instead of a fixed percentage the algorithm
compares the latest price move against how much this asset usually swings right now.
If a quiet asset suddenly jerks, that is an alarm. If the asset jumps by that much
all the time, that is routine, not an alarm.

Between notifications on one asset there is a pause (14 days by default), so the chat
does not fill up with repeats about the same event. But if something genuinely large
and new happens during that time, the notification still arrives — the pause will not
make you miss something that really matters.

Each asset is in fact covered by two independent signals — the hourly one (described
above) and a daily one, which catches a slow multi-hour drift in one direction where
no single hour looks anomalous but the total move over a day does. It has its own
short (2-day) pause between notifications on one asset — details in the "Daily signal"
section below. The Telegram message itself always says which of the two fired.

If you want notifications more or less often, two parameters in `config/config.yaml`
control that: `price_zscore_threshold` (the price threshold) and
`volume_zscore_threshold` (the volume threshold). For some assets a noticeable share
of notifications comes from volume rather than price, so it is worth changing both
rather than only the first. The smaller the values, the more often alerts fire; the
larger, the rarer.

There is one more, optional capability: on request, ask an LLM to explain an alert
that has already been sent using fresh news and append the reason to that same
Telegram message. It is not part of the ordinary hourly cycle — it is run manually and
needs separate setup, details in the "Explaining an alert with an LLM and news"
section below.

How all of this works inside is in the sections below. Reading them is not required
for everyday use: they are for anyone curious about the details or wanting to tune
things finely.

<details>
<summary><b>How an "anomaly" is actually computed</b></summary>

Instead of a simple daily percentage, two independent signals are used:

1. **EWMA volatility** — an adaptive estimate of how much the asset is swinging right
   now. It adjusts quickly to a change of regime: if the market has been stormy for
   several days, a wider range temporarily counts as "normal".
2. **A robust z-score on median/MAD** over a rolling window — a more conservative,
   longer-lived estimate of normal. Unlike an ordinary standard deviation, one past
   jump does not inflate it. Candles with no price change (a stuck quote — the typical
   situation in quiet FX hours) are deliberately kept out of that window: otherwise an
   ordinary move starts to look like an extreme outlier.

By default a price alert fires only if **both** signals exceed the
`price_zscore_threshold` at the same time — this cuts off chance firings of one
method. The weak spot of such a confirmation: if one signal is extreme by itself, the
other may lag just below the threshold and the event is lost for nothing. Hence a
higher threshold, `price_zscore_override`: if at least one signal crosses it, the
other's confirmation is no longer required. Volume works the same way — normally it
needs confirmation from a price move (`volume_zscore_threshold`), and with extreme
volume (`volume_zscore_override`) it does not.

The pause between notifications is not a rigid timer either. A repeat alert during
the pause will still be sent in two cases: if it is `escalation_factor` times larger
than the one that caused the previous alert, or if it is extreme in itself (crossing
`price_zscore_override`/`volume_zscore_override`) — then the pause is ignored
entirely. The logic for this part is in `price_monitor/state.py`. Per-asset state is
kept in `data/state.json` and committed back to the repository after every run.

</details>

<details>
<summary><b>Daily signal</b></summary>

The hourly signal above compares one hour's move against how much the asset usually
changes in an hour. That has a blind spot: a slow trend stretched over many
consecutive hours, where each individual hour looks entirely ordinary while the total
move over a day does not. No hourly z-score will cross the threshold in that
situation, because each hour genuinely is unremarkable on its own.

The daily signal is an independent second detector of the same construction (EWMA +
robust z-score, `analyze()` — the very same code), only on daily candles instead of
hourly ones. Daily candles are not requested from the source separately — that would
cost extra requests against Twelve Data's already tight limit (see "Data-source
quirks"). Instead they are built locally, every hour, from the price history already
accumulated (`data/candle_history/`, see below) — taking the close of the last hour of
the UTC day. The day boundary is always 00:00 UTC, the same for every asset and every
source, regardless of how each exchange defines "its" trading day.

The signal is recomputed on every hourly run rather than once a day on a schedule —
but that is not a problem: the value of the last closed day does not change until the
next one arrives, so recomputing within the same day simply produces the same result,
and the pause between notifications (see below) keeps that from turning into spam.

**It is calibrated differently from the hourly signal — against recall, not against a
target frequency.** The hourly signal is tuned to a specific frequency (0-1/2-3 a week
per group). That logic would not fit the daily one: the goal is not "how many
notifications a week" but "did we catch the largest moves of this particular asset".
The threshold for each asset is picked separately
(`python -m price_monitor.backtest --local-history --calibrate-daily-recall`) so as to
catch roughly half of the top-N largest historical moves of that asset, where
N ≈ the number of months of available history (not a fixed five — with 4-5 years of
local history this gave N between 23 and 68 depending on the asset). The actual result
across all 16 assets: thresholds landed in the 2.2–2.9 range (noticeably below the
original 4.0 placeholder), recall 46-52% for nearly every asset, 383/780 across the
portfolio in total. The thresholds are per-asset overrides in each asset's entry in
`config.yaml`; the global values are only a fallback for a new asset with no
calibration of its own.

The pause between daily notifications for one asset (`daily_cooldown_minutes`, 2 days)
is deliberately shorter than the hourly one (14 days): `escalation_factor` already
lets a genuinely intensifying move through the pause, so a long pause is not needed
here — a couple of days is enough to avoid duplicating a notification about the
continuation of a move of the same size. Importantly, the pause is strictly
**per-asset**, not shared across several assets — if 5 different assets fire on the
same day (say they all reacted to the same macro news), each still sends its own
separate notification to Telegram. This is a deliberate choice: a portfolio-wide pause
was tried and rejected — a shared pause could suppress a second, independent and more
important event only because the first (however local and less significant) had
already "taken" the pause. Since delivery is always per-asset, you can always request
an explanation for any asset that fired, even if it was not the first of a series of
similar firings on the same day.

Because each asset is delivered independently, a correlated move (the whole FX group
at once, say) can produce several notifications in a row in the moment — that is an
accepted trade-off, not an oversight: for statistics (not for delivery) there is a
separate diagnostic in `backtest.py` — firings of different assets within 2 days of
each other are counted as a single "event" when computing frequency, so that a
correlated burst does not inflate that figure artificially. On real data across the
whole portfolio this gave 217 deduplicated events over ~5.5 years (~0.75/week) — the
backtest prints it for reference only, and it has no effect on what is actually sent
to Telegram.

The daily signal is not a replacement for the hourly one but a complement: both run in
parallel on all assets, not only on currency pairs (the blind spot is structural and
does not depend on the asset class).

To decide whether these thresholds really catch what is needed (rather than simply the
right percentage of history), there is a separate manual tool —
`python -m price_monitor.calibration_review --signal daily`: it takes every event from
each asset's top-N (both caught and missed) and pulls 5 real news headlines from the
window [event+6h, event+12h] — the same logic already used to explain live alerts (see
"Explaining an alert with an LLM and news" below), only applied to historical dates. If
fewer than 3 headlines are found in that window, the tool additionally tries a later,
non-overlapping window [+12h, +24h] and adds whatever it finds there — the real news
sometimes only comes out after the first news cycle, while the main window [+6h, +12h]
itself is left alone and not widened (it works well as it is when the coverage is
there). The result is a readable markdown report
(`data/calibration_review/daily_signal_review.md`) for manual review: from it you can
judge whether the threshold should move up or down, by looking at which real news
landed among the caught events and which among the missed. The tool is signal-agnostic
(it works with `--signal hourly` too), but so far has only been run for the daily
signal.

</details>

<details>
<summary><b>Local history of prices and monitoring decisions</b></summary>

Besides `data/state.json` and `data/alerts_log.json`, the bot keeps two more
permanent, never-truncated journals — one file per asset in each, in NDJSON format
(one JSON object per line), so that every hourly run only appends a new line at the
end rather than rewriting the whole file — that way git diffs stay small and readable
even years later:

- **`data/candle_history/`** — the entire price history the bot has ever received from
  the sources, one file per asset (`source_symbol.ndjson`). The daily signal is built
  from it (see above), but it is also useful simply as a long private archive of
  quotes — history significantly longer than a few years is not always easy to find
  for free on many assets. It is filled in two ways: gradually, one candle an hour, and
  in bulk — any run of `price_monitor/backtest.py` also merges into the local history
  everything it managed to download for the backtest (no duplicates, keyed by candle
  time), so actually running a backtest is the fastest way to fill the archive months
  ahead instead of waiting for it to accumulate an hour at a time.
- **`data/decision_log/`** — not only the alerts that were sent (that is
  `alerts_log.json`) but every computed value of both signals, on every run, for every
  asset: the z-scores, whether an alert fired, the thresholds used, and whether a
  notification ended up being sent (after the pause/escalation) or not. It exists so
  that for any moment in time you can afterwards reconstruct what exactly the detector
  saw and why it decided the way it did — not only when an alert arrived, but also when
  it did not, although one might have thought it should.

The growth of these two journals over time is deliberately not addressed now — if it
ever becomes a real problem (rather than a hypothetical one), that is when rotation or
archiving is worth doing, not earlier.

A one-off deep backfill is done with `--since` instead of `--days`:

```bash
python -m price_monitor.backtest --since 2021-01-01
```

Locally this is convenient for assets that need no key (Coinbase, Yahoo). Currency
pairs need `TWELVEDATA_API_KEY` — rather than pasting the key into a chat or into a
local environment, there is a separate workflow for that: **Actions → Backfill
Candle History → Run workflow** (the `since` field, `2021-01-01` by default). The key
is used only inside the workflow — a GitHub Actions secret fundamentally cannot be read
back, neither through the API nor any other way, only referenced from a workflow, which
is exactly what this step does. It commits the result itself into
`data/candle_history/`, just as `price-monitor.yml` already does for `state.json`.

This workflow carries the same `concurrency: group: price-monitor` as
`price-monitor.yml` — deliberately: both use the same Twelve Data key and a shared
limit of 8 credits per minute across the whole account, not per process. Without a
shared group the external hourly trigger could coincide with a backfill and the two
sets of requests together would exceed the limit — which is exactly what happened once
in practice (both runs failed with 429 at the same time). And the commit step in this
workflow carries `if: always()`: if the backfill dies halfway through (having
exhausted the daily limit of 800 requests, for example), whatever already merged into
the local history for earlier assets in that same run is still committed rather than
lost along with the runner.

Unlike `--days N` (a sliding window "N days back from the moment of the run"),
`--since` is a fixed calendar date: rerun a year later it will not "drift", it will
simply ask for a year more history. For assets on Coinbase and Twelve Data this really
does fetch history back to 2021 (respecting their rate limits — the eight currency
pairs can take about 10-15 minutes). For assets on Yahoo the request is automatically
trimmed to the maximum the source will serve (~2 years, see "Data-source quirks" —
that is Yahoo's own limit, not ours). The same principle applies to new assets added
later: do a one-off `--since 2021-01-01`, and the source will return either everything
back to that date or everything it has at all, if the asset is younger or the source
cannot go that deep (as with Yahoo). Routine backtest runs for threshold tuning stay on
`--days` (365 by default) — there is no point re-downloading years of history every
time when the local archive has already accumulated it.

</details>

<details>
<summary><b>Weekly economic calendar digest</b></summary>

Every Saturday around 12:00 Israel time the bot sends into the same chat, as a
separate message, a list of the main economic events of the coming week (NFP, central
bank meetings, inflation and so on) — simply so that you know in advance which days to
expect heightened volatility, before it has already happened.

The data comes from ForexFactory's public JSON feed
(`https://nfs.faireconomy.media/ff_calendar_thisweek.json`), no key needed. The feed
only has a "this week" variant (`nextweek`/`lastweek` were checked live and do not
exist — both 404), and it covers Sunday through Friday.

**The sending day is not chosen but checked.** Only one thing has been confirmed
live: on Sunday the request returns exactly the coming week. For Saturday this
remained a guess — both possible week boundaries at the source (Sunday-to-Saturday and
Saturday-to-Friday) are equally consistent with what the feed returns on a weekday, and
they can only be told apart by a request on a real Saturday. So there are two windows,
Saturday and Sunday around 12:00 Israel time, and the digest goes out in the first of
them where the feed really does look forward (the feed's last event is still ahead). If
Saturday returns the week that is ending, the message waits a day. It will not go out
twice: the deduplication key is taken from the feed itself — from the date of its first
event — and for a Saturday and a Sunday that returned the same week it is identical.
Only events with `impact: Medium` and `impact: High` make it into the digest — `Low`
and `Holiday` are filtered out entirely. There is no LLM here and none is planned:
every event already carries an importance label from the source, there is nothing to
add — just a readable list.

The message is grouped by day, and under each event come all the values the source
returned: **actual · forecast · previous**. Empty fields are skipped rather than
printed as dashes — a forecast exists for roughly 70% of events, a previous value for
80%, and a line of three dashes would only tell you that the source said nothing.
The actual in a week-ahead digest is empty by construction: the events have not
happened yet — it shows up on a manual run over a week that has passed.

A long week is split across several messages on a day boundary: Telegram rejects a
message longer than 4096 characters outright rather than truncating it, and without
splitting the digest simply would not arrive. Event titles are escaped — a single
`S&P Global PMI` without escaping would be enough for Telegram to reject a message
with `parse_mode=HTML`.

No separate schedule was set up for this: `weekly_digest.py` is called from
`__main__.py` on **every** hourly run (the same external cron-job.org trigger as
always — see "Data-source quirks" below for why it has no GitHub Actions schedule of
its own), but it does nothing at all in any hour except the two that fall on Saturday
and Sunday ~12:00 Israel time. That the digest for this week has already been sent is
recorded in `data/state.json` — if the external trigger happens to fire twice in that
hour (or a few minutes either side of the hour boundary), it will not be sent again.

To see the digest without waiting for Saturday (to check the mechanism itself, or
simply to look at the coming week right now) — **Actions → Test Weekly Digest → Run
workflow**, or locally `python -m price_monitor.weekly_digest --force`. `--force`
bypasses both the day/time check and the "already sent this week" flag — the state in
`state.json` is not touched at all, so this has no effect on the real Sunday send.

Every such run also saves the events it received from the feed into
`data/economic_calendar/calendar.ndjson` — the same way as `candle_history` (NDJSON, no
duplicates). The archive is needed both by `calibration_review.py` (context for "what
was on the calendar on the day of the jump") and by the MEALS calendar multiplier
(§4.3).

**Backfilling the actual.** The live weekly feed does not return the `actual` field at
all — verified against its output, its keys are: `country`, `date`, `forecast`,
`impact`, `previous`, `title`. An event enters the archive a week before publication,
with a forecast and a previous value, and the released figure would never appear: the
feed never comes back to that event. So the same Saturday run reads back **the current
and the previous month** from ForexFactory's monthly pages, where the actual is present
(85% of events for August 2026, 77% for March 2021). The previous month is needed for
events at the very end of a month whose actual is released in the new one, and for the
case where the source revises a figure after the fact. Two requests a week.

Deduplication goes by the **moment** of publication, not by the date string: the weekly
feed writes `2026-09-04T08:30:00-04:00`, the monthly page
`2026-09-04T12:30:00+00:00`, and as strings those are two different events. The merge
updates a record if even one of its fields changed, not only when a new row was added —
otherwise a backfilled actual would be silently thrown away along with the whole record
on its way to disk.

**The archive keeps every `impact` level — Low/Medium/High**, even though the Telegram
digest still shows only Medium+High (that is about what to expect in the week ahead,
not about what is worth keeping for backtests after the fact).

**The historical part of the archive.** ForexFactory itself has no ready-made feed for
past dates — only "this week". But the **monthly pages** are readable
(`forexfactory.com/calendar?month=sep.2026`), and the whole history now rests on them.
The subtlety is in the client, not in the headers: `requests` gets a 403 from those
pages with any set of headers, a full browser set included, while `urllib` with the
same User-Agent gets a 200. The difference is in the TLS fingerprint, and no headers
will argue it down. Weekly scraping (`calendar?week=...`) is still closed off by
Cloudflare (verified live: HTTP 403, a "Just a moment..." JS challenge) — it is exactly
what created the long-standing impression that ForexFactory history is unobtainable.

The data on a monthly page sits in the markup as ready-made JSON, and the time in it is
a unix timestamp, that is, unambiguous. The very ambiguity that spoiled all previous
attempts is absent here by construction.

A full rebuild is `--rebuild`, about 69 requests with a two-second pause, some four
minutes:

```bash
python -m price_monitor.economic_calendar --rebuild
```

(or **Actions → Backfill Economic Calendar → Run workflow**, mode `rebuild`.)
A separate range can be appended without touching the rest:
`--import-forexfactory --from-month 2024-01 --to-month 2024-06`. Everything older than
`2021-01-01` (`_ARCHIVE_SINCE`) is dropped before writing: there is no candle history
before that date, so there is nothing to match such an event against.

**Why one source and not several.** Third-party sources were all tried and all
dropped — one at a time and each for a measured reason.

- **Three ready-made dumps** — `Ehsanrs2/Forex_Factory_Calendar` on Hugging Face
  (2007–2025, all levels), `ehsanrs2/forexfactory-scraper` on GitHub (High only),
  `spoluan/forex-factory-scraper` (2010–2023). The archive assembled from them
  contained 25% duplicates among High and Medium: the same event twice within a day,
  with a dominant shift of exactly seven hours. The dumps were made under different
  timezone conventions, and the merge key included the date. Every US release turned
  out to have two clusters of times instead of one.
- **Financial Modeling Prep** — the Economic Calendar is paid even on the "stable"
  plan (verified with a real key: HTTP 402).
- **QuantGist** — only 30 days of history for free.
- **MetaTrader 5 Python API** — the calendar is poorer and requires an installed
  terminal.
- **FRED** — it has macroeconomic series but no calendar of releases.
- **Kaggle, "Global Economic Calendar"** (EL Younes, CC BY-NC-SA 4.0, 2020–2025) — its
  timestamps were fine, but its **taxonomy** was not. It handed out the `Medium` label
  nine times more generously than ForexFactory itself: 96.9 events a week against 11.3
  over the same period, while `High` matched for both (13.0 and 13.4). An archive glued
  together from Kaggle and ForexFactory got a seam exactly where one gave way to the
  other: the MEALS calendar multiplier (§4.3) was switched on in **90.7%** of hours on
  the Kaggle half and in **53.2%** on the ForexFactory half. For calibration that is
  worse than missing data — the train period would lie entirely in the generous half
  while live work ran on the stingy one.

The price of a single source is fewer `Low` events: ForexFactory has an order of
magnitude fewer of them than Kaggle did. In substance that price is zero: the §4.3
multiplier uses only `High` and `Medium`, and `Low` plays no part in it at all.

</details>

<details>
<summary><b>Verified by a backtest on real historical data</b></summary>

The script `price_monitor/backtest.py` runs exactly the logic used in production over
every hour of real history for a chosen period. It does not merely check whether the
signal would have fired — it simulates whether a notification would actually have been
delivered, taking the pause between alerts into account. Along the way it runs the same
simulation for the daily signal (building daily candles from the same downloaded
history) and prints a second summary table, and it tops up `data/candle_history/` as it
goes — see "Local history of prices and monitoring decisions" above.

```bash
python -m price_monitor.backtest --days 365 --out data/backtest_results.json
```

Here is what came out of a run on 16 assets over roughly a year with the current
thresholds:

| Asset | Notifications per week | Recall |
|---|---|---|
| Bitcoin | 0.19 | 2/5 |
| Ethereum | 0.29 | 4/5 |
| Solana | 0.25 | 3/5 |
| Gold | 0.27 | 3/5 |
| WTI crude | 0.34 | 4/5 |
| S&P 500 | 0.22 | 2/5 |
| 30Y Treasury | 0.25 | 3/5 |
| EUR/USD | 0.17 | 1/5 |
| GBP/USD | 0.17 | 1/5 |
| USD/JPY | 0.39 | 5/5 |
| USD/CHF | 0.17 | 3/5 |
| USD/CAD | 0.13 | 0/5 |
| AUD/USD | 0.09 | 1/5 |
| NZD/USD | 0.17 | 3/5 |
| USD/CNY | 0.36 | 3/5 |
| Dollar index (DXY) | 0.21 | 4/5 |

In total, about **3.7 notifications a week**. The frequency target (0-1 a week in quiet
times, 2-3 in turbulent ones) is now measured not across all 16 assets at once but
separately for two groups: the currency group (8 pairs + DXY — all of them essentially
about dollar strength or weakness, one macro event moves the whole group at once) and
everything else (crypto, commodities, indices, bonds). At the current threshold each
group on its own fits that target (~1.9/week and ~1.8/week) — the single figure for
everything at once was written back when there were nine assets, and as their number
grew it simply stopped being meaningful; the threshold has nothing to do with it.

Recall (**42 out of 80**) is the share of each asset's top-5 largest moves over the
year that the bot would actually have caught; it is supposed to be lower when tuned for
rarity rather than for maximum coverage. Some of the "misses" are not a lost signal but
several jumps of one turbulent episode collapsed by the pause into a single
notification.

For some currency pairs (GBP/USD and USD/CAD especially) recall is low — 0/5-1/5 — and
that, unlike previous findings, **is not fixed by the threshold**: checked with a
separate sweep from 3.0 to 7.0 — GBP/USD stays at 1/5 recall even at a threshold of
3.0, where the pair itself is already noisy at 0.7 alerts a week. So it is not a
calibration matter: the largest moves of these particular pairs, in percentage terms,
statistically do not look unusual relative to their own recent volatility — apparently
they happen against an already elevated spread rather than a quiet background. Lowering
the threshold will not help, it will only add noise for the other pairs in the group, so
the thresholds are left as they are.

Along the way the backtest found and helped fix a couple of real data problems — stuck
quotes on USD/CNY (back when it went through Yahoo) and structurally broken volume on
the S&P 500 (details in `analysis.py` and `config.yaml`).

The full version of the report with charts is on the
[backtest dashboard](https://claude.ai/code/artifact/272f70f2-5d26-4a94-b8a4-75a8c7ff69db)
(a private link; it can be shared through the Share menu on the page).

</details>

<details>
<summary><b>Data-source quirks</b></summary>

| source       | what it covers                              | symbol format                   | example             |
|--------------|---------------------------------------------|---------------------------------|---------------------|
| `coinbase`   | crypto spot (Coinbase Exchange)              | `BASE-QUOTE`                    | `BTC-USD`           |
| `yahoo`      | futures, indices (Yahoo Finance)             | ticker as on finance.yahoo.com  | `GC=F`, `DX-Y.NYB`  |
| `twelvedata` | currency pairs (Twelve Data)                 | `BASE/QUOTE`                    | `EUR/USD`           |

- **FX pairs have no volume.** For spot currency pairs no single exchange volume exists
  at any provider — Twelve Data always sends `volume=0`. For such assets only the price
  signal works; that is expected behaviour, not a bug.
- **Futures sometimes "jump" on a contract roll.** Tickers of the form
  `GC=F`/`CL=F`/`ES=F`/`ZB=F` are the "continuous" front-month contract. On expiry
  Yahoo switches it to the next one itself, and at that moment a price gap unrelated to
  any real market move is possible. This happens a couple of times a year per asset.
- **Yahoo's hourly candles are hard-limited to 730 days back.** Verified live: a request
  for hourly bars beyond the last 730 days returns an explicit error (HTTP 422, "The
  requested range must be within the last 730 days") — that is not a soft limit and
  cannot be worked around. So assets on Yahoo (gold, oil, the S&P 500 e-mini, the 30Y
  Treasury, DXY) physically cannot accumulate hourly history deeper than ~2 years back,
  unlike Coinbase and Twelve Data — `backtest.py` (`fetch_backtest_history`) trims an
  over-long request to 729 days itself, rather than failing with a 422 and bringing the
  whole run down.
- **The Yahoo Finance API is unofficial.** An undocumented chart endpoint is used —
  no official free API for futures and indices exists. It has worked steadily for many
  years, but Yahoo could restrict it without warning. If assets with `source: yahoo`
  suddenly start failing constantly, that is the first thing to check. FX does not
  depend on this risk — it goes through a separate provider, so one failing does not
  stop the other.
- **Twelve Data's limit is 8 requests a minute, 800 a day on the free tier.** That is
  exactly why there are eight currency pairs and no more: that uses the entire
  per-minute limit in one run. Adding a ninth pair without removing one of the current
  ones is impossible on the free tier — see the checklist below. By default Twelve Data
  returns times not in UTC (the local time of the "exchange", which for FX is not UTC),
  so the code passes `timezone=UTC` explicitly — without it every candle would be
  shifted by several hours relative to all the other sources.
- **Binance is not used.** It was originally considered as a crypto data source, but its
  public API returns HTTP 451 (a geo-block) from US IP addresses — which is exactly
  where standard GitHub Actions runners usually are.

</details>

<details>
<summary><b>Per-asset thresholds</b></summary>

Any parameter can be overridden for a specific asset right in `config/config.yaml` —
it is enough to add the key to its entry. This is how the volume channel is switched
off for the S&P 500, for example:

```yaml
assets:
  - symbol: "ES=F"
    source: yahoo
    label: "S&P 500 (E-mini futures)"
    volume_zscore_threshold: 200.0   # effectively disables the volume channel
    volume_zscore_override: 200.0
```

Any of these keys can be overridden: `interval`, `lookback`, `mad_window`,
`ewma_lambda`, `price_zscore_threshold`, `price_zscore_override`,
`volume_zscore_threshold`, `volume_zscore_override`, `volume_min_price_move_z`,
`cooldown_minutes`, `escalation_factor`, `min_history`, and for the daily signal (see
"Daily signal" above) — `daily_mad_window`, `daily_ewma_lambda`,
`daily_price_zscore_threshold`, `daily_price_zscore_override`, `daily_min_history`,
`daily_cooldown_minutes`, `daily_escalation_factor`. Anything not overridden for a
specific asset is taken from the shared settings in the same file.

Thresholds are configured on three levels, from general to specific:

1. The default value in the code.
2. The value from `config.yaml`, if set there.
3. An environment variable (the same names in upper case, plus
   `HEALTH_ALERT_AFTER_FAILURES` and `HEALTH_REMINDER_EVERY_FAILURES`).
4. An override on a specific asset in `config.yaml`.

Each level overrides the previous one.

</details>

<details>
<summary><b>An alert when the monitoring itself fails</b></summary>

If fetching data or sending to Telegram fails several runs in a row (three by default,
configurable through `health_alert_after_failures`), a separate "monitoring is down"
message arrives with a list of reasons. Until the problem is fixed, a reminder arrives
from time to time — once a day by default (`health_reminder_every_failures`) rather
than on every run. When everything works again, a "monitoring has recovered" message
arrives.

There is one case this will not cover: if `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID`
themselves are wrong, or the bot is blocked, then reporting that through the same
Telegram is of course impossible. The only signal then is a red ❌ on the workflow in
the Actions tab.

</details>

<details>
<summary><b>Explaining an alert with an LLM and news</b></summary>

Every alert that is sent is always saved into `data/alerts_log.json` — together with
its Telegram message id, so that it can be edited later. That happens by itself and
depends on nothing. Appending a probable reason to the alert ("why the asset fell or
rose"), on the other hand, happens only on request, as a separate step:

1. Get an API key from any provider with an OpenAI-compatible API
   ([DeepSeek](https://platform.deepseek.com/) is configured by default — cheap and
   without unnecessary formalities). Top up the balance there — billing is by tokens,
   and without a key the step will not work.
2. Add the key under **Settings → Secrets and variables → Actions → Secrets**, under
   any name — `DEEPSEEK_API_KEY`, for example.
3. In `.github/workflows/explain-alerts.yml` say which secret to use, in the line
   `LLM_API_KEY: ${{ secrets.YOUR_SECRET_NAME }}`.
4. To explain a particular alert: **Actions → Explain Alerts → Run workflow**, and in
   the **Message ID** field the number from the `ID: 12345` line at the end of the
   Telegram message. The field is deliberately mandatory: otherwise one run could
   accidentally spend tokens on the whole accumulated queue rather than on a single
   alert.

The bot takes the specified alert, searches for headlines through Google News (no key
needed) and asks the LLM to connect the news to the price move in 2-3 sentences in
Russian — with no suitable news the model is required to say honestly that no reason
was found, rather than inventing one. The explanation is appended to the same message
by editing it; no new message is created. Along with the answer itself,
`data/alerts_log.json` stores the model (`llm_model`) and the exact request sent to it
(`llm_messages`, including every article the model actually saw) — if a strange answer
comes back, you can check what was actually sent to the LLM without waiting for the
Actions run log to expire.

News is searched strictly in the window **from 6 to 12 hours after the alert** — a
fixed stretch relative to the alert itself, not "from the alert until now" (that way
the result does not depend on when the step is run). Such a window almost always
captures the relevant news without being diluted either by too-fresh guesswork (in the
first hours there is usually only the fact, without any analysis of causes) or by
random later events off the topic. The step will not touch an alert until
`explain_min_age_hours` hours have passed (12 by default — the end of the window);
younger alerts wait for the next run.

The window boundaries are passed to Google through the `after:`/`before:` operators.
Verified live: Google reads those dates not in UTC but in US Pacific time
(America/Los Angeles, apparently because of `hl=en-US`/`gl=US`) — the dates are
converted into that zone through `zoneinfo` before the request, with no eyeballed
safety margin.

This is a tool on demand, not automation — it costs a little money in tokens, which is
why it is not built into the hourly monitoring cycle.

Changing the LLM provider requires no code edits: `price_monitor/llm.py` works with any
service that has a `/chat/completions` endpoint in the OpenAI format (DeepSeek, OpenAI,
OpenRouter and most others). Only `llm_base_url`/`llm_model_peak`/`llm_model_offpeak`
in `config.yaml` and the secret in `LLM_API_KEY` change (step 3 above).

By default two DeepSeek models are used depending on the time of day — it works out
cheaper. During "peak" hours (01:00–04:00 and 06:00–10:00 UTC on weekdays =
04:00–07:00/09:00–13:00 Israeli summer time, 03:00–06:00/08:00–12:00 winter time) the
price is full, so the cheap `deepseek-v4-flash` is used; the rest of the time the price
is half, so the stronger `deepseek-v4-pro` is used, which with the discount costs about
the same. The same schedule is repeated in the description of the Message ID field in
the Run workflow form. If your provider has no peak-hour split, put the same model in
both settings.

For every asset whose search-friendly name does not match its `label` (for example
"Gold (Gold futures)"), `config/config.yaml` has an optional `news_query` field — an
ordinary text query for the news search.

</details>

<details>
<summary><b>A reliable hourly trigger</b></summary>

GitHub Actions' own schedule (`schedule:`) is unreliable on low-activity repositories:
instead of the declared hour, a job may genuinely run once every 5-10 hours —
documented GitHub behaviour ("best effort", not a guarantee), not a bug in this
project. So `price-monitor.yml` no longer has a schedule at all, only
`workflow_dispatch`, and an external free service pokes it once an hour.

1. Create a **fine-grained personal access token** at github.com →
   Settings → Developer settings → Personal access tokens → Fine-grained tokens.
   Repository access → this repository only. Permissions → **Actions: Read and
   write** — nothing else needs ticking. Set an expiry (fine-grained tokens cannot be
   perpetual — it will need reissuing once a year).
2. Register at [cron-job.org](https://cron-job.org/) (free, no card) and create a job:
   - URL: `https://api.github.com/repos/YOUR_ACCOUNT/YOUR_REPOSITORY/actions/workflows/price-monitor.yml/dispatches`
   - Method: **POST**
   - Headers: `Authorization: Bearer YOUR_TOKEN` and `Accept: application/vnd.github+json`
   - Request body: `{"ref": "BRANCH_NAME"}` (the branch the workflow lives on)
   - Schedule: hourly.

The token only grants the right to run workflows in this one repository — it cannot
read the code, the secrets or anything else.

</details>

<details>
<summary><b>Project structure</b></summary>

```
price_monitor/
  config.py      — loads config/config.yaml + overrides from env/secrets
  coinbase.py    — public REST client for Coinbase Exchange (crypto), no key needed
  yahoo.py       — public chart client for Yahoo Finance (futures/indices), no key needed
  twelvedata.py  — Twelve Data client (currency pairs), needs a free key
  market_data.py — picks the right client from an asset's source field
  models.py      — shared types (Candle, ExchangeError)
  analysis.py    — EWMA volatility, robust z-score, override, alert logic
  candle_store.py  — permanent local price history + assembling daily candles
  decision_log.py  — a log of every computed decision of both signals (not only alerts)
  notifier.py    — sending and editing Telegram messages
  state.py       — the pause between price alerts, escalation included
  health.py      — an alert when the monitoring itself fails
  alerts_log.py  — a log of sent alerts awaiting an LLM explanation
  news.py        — searching fresh news for an asset (Google News, no key needed)
  llm.py         — a client for any OpenAI-compatible LLM provider
  explain.py     — entry point of the "explain alerts with an LLM and news" step
  backtest.py    — walk-forward backtest of the thresholds (hourly and daily signal) and
                   a simulation of real alert delivery; also tops up candle_history
  calibration_review.py — a manual tool: pulls real news for events from the backtest
                   (both caught and missed), for reviewing the calibration by hand
  economic_calendar.py — ForexFactory feed client + local NDJSON archive of events
  weekly_digest.py — a digest of Medium/High calendar events in Telegram (Sat/Sun)
  test_notify.py — a manual check of Telegram delivery, without a real alert
  __main__.py    — entry point of a single monitoring run (both signals + the digest)

config/config.yaml         — the asset list and the thresholds
data/state.json            — persistent state (committed back to the repo)
data/alerts_log.json       — the alert log for the explanation step (committed back to the repo)
data/candle_history/       — permanent local price history, one file per asset
data/decision_log/         — a log of every decision of both signals, one file per asset
data/calibration_review/   — markdown reports of the manual calibration review (not committed)
data/economic_calendar/    — local archive of calendar events (committed back to the repo)

.github/workflows/
  price-monitor.yml     — workflow_dispatch (the hourly trigger is provided by an
                          external service, see "A reliable hourly trigger" above)
  test-notify.yml       — a manual run of test_notify.py from the Actions tab
  explain-alerts.yml    — a manual run of explain.py from the Actions tab
  weekly-digest-test.yml — sends the calendar digest right now
                          (`weekly_digest.py --force`), without waiting for
                          Saturday — does not touch "already sent this
                          week" in state.json
  backfill-history.yml  — a manual deep top-up of data/candle_history/ (needed for
                          the currency pairs — it works with the TWELVEDATA_API_KEY
                          secret without revealing it)
  backfill-calendar.yml — a manual rebuild of data/economic_calendar/ from
                          ForexFactory's monthly pages (~69 requests,
                          all history from 2021-01) — no key needed
  tests.yml             — unit tests on push/PR

tests/ — pytest tests for every module above
```

</details>

## MEALS — the new alert logic (in development)

Alongside the current monitoring, MEALS (Macro-Event Alert & Logic System) is being
built to a separate technical specification, version 5.1. This is not a refinement of
the existing signals but a different construction: instead of sixteen independent
detectors, a cross-sectional analysis of the whole basket (the Composite Sensation
Index) plus a separate module for single-asset moves cleaned of the common market
factor.

**The current monitoring's alerts are muted right now** — `alerts_muted: true` in
`config/config.yaml`. The hourly run proceeds as usual: quotes are downloaded,
`data/candle_history/` and `data/decision_log/` keep filling, the Saturday calendar
digest goes out, a notification about the bot itself breaking goes out. The only thing
silenced is the per-asset messages — the very signals MEALS is replacing.

Muting does not spend the cooldown: the state stays as if there had been no signal, so
once it is lifted the first genuine move gets through rather than running into a pause
accumulated during the silence. To lift it, set `false` (or set `ALERTS_MUTED=false` in
the run's environment).

Why a flag in the repository rather than a disabled scheduler on the cron-job.org side:
"we are deliberately silent" is a state of the project, and it should be visible in the
same place as the code. A cron switched off on someone else's site is indistinguishable
from a breakage a month later, and the data for that period would not have accumulated
at all. MEALS is being assembled in the `meals/` package and will only take over after
a backtest — see the rollout plan.

What exists so far (phases 0–6: data, computation, events, journal and export):

```
config/basket.yaml   — basket composition: 21 assets, five blocks, tiers, price steps
meals/basket.py      — loading the basket, weights by the equal-weight rule
meals/bars.py        — the hourly bar store (Parquet) and assembling the hourly grid
meals/sessions.py    — the NYSE session calendar and the basket's reference calendar
meals/windows.py     — the registry of windows and constants, three incompatible time units
meals/quality.py     — the bar quality gate and whether an hour belongs to a session
meals/returns.py     — returns, the gap channel of the first bar of a session, winsorization
meals/zscore.py      — the out-of-sample EWMA Z-score and adaptive Q95/Q99 thresholds
meals/volume.py      — a robust volume profile by local exchange hour
meals/pipeline.py    — per-asset metrics, the whole phase 1-2 chain in one pass
meals/cross_section.py — the hour's quorum, M_t, basket dispersion, PCA, single-factorness
meals/residuals.py   — regression on the basket factor and the block factor, the residual series
meals/saed.py        — single-asset events from the residuals and block alerts
meals/calendar_multiplier.py — an hour's importance multiplier from the economic calendar
meals/vix.py         — the stress multiplier from the daily VIX series
meals/si_index.py    — base points and the Composite Sensation Index
meals/cluster.py     — cluster events: the gate, the cooldown, escalations
meals/versioning.py  — config_version and run_version, run idempotency
meals/journal.py     — the decision journal and the trigger readiness table
meals/export.py      — exporting a cluster event to JSON under a fixed schema
meals/corporate_actions.py — ex-dividend dates derived from the quotes
meals/fred.py        — the daily VIX series from FRED and the moment it becomes available
meals/backfill.py    — the one-off load of history from 2021
meals/audit.py       — the data coverage table, required by §2.1 of the spec
meals/truth.py       — the §7 yardstick: truth labels by block and the SPY baseline
meals/evaluate.py    — precision, recall, F1, lead time and the §7 diagnostics
meals/calibrate.py   — the §7 fit on train, folded and frozen before test is read

data/meals/bars/     — hourly bars per instrument
data/meals/vix/      — the daily VIX series
data/meals/sessions/ — the NYSE schedule: trading days and half sessions
data/meals/corporate_actions.csv — ex-dates for the funds
data/meals/metrics/  — per-asset metrics (metrics_asset_hour)
data/meals/metrics_basket_hour.parquet — basket metrics by hour
data/meals/saed_events.parquet — single-asset events
data/meals/saed_block_alerts.parquet — block alerts
data/meals/cluster_events.parquet — cluster events
data/meals/cluster_event_escalations.parquet — escalations inside events
data/meals/residuals/ — residual series per instrument (not in the repository, see below)
data/meals/decision_log.parquet — the decision journal: magnitude, threshold, outcome, versions
data/meals/first_valid_hour.parquet — from which hour a trigger can be trusted
data/meals/truth_labels.parquet — §7 labels: was the next 24h significant, and the baseline
data/meals/truth_thresholds.parquet — the per-block Q99 those labels rest on, taken on train
data/meals/evaluation.md — the backtest report: the detector against the §7 baselines
data/meals/calibration.json — the search: what was tried, what was chosen, how it held up
data/meals/frozen.json — the frozen config_version and the train result behind it
data/meals/events/   — event export in JSON (not in the repository, see below)
data/meals/coverage.md — the coverage table
schema/event_export.schema.json — the event export schema, the contract for a consumer
docs/TZ_MEALS_v5.1.txt — the specification itself: what every "§4.3" in the code points at
docs/meals-deviations.md — departures from the spec, each with its reason
```

**The specification is superseded.** It is kept because the code cites its section
numbers in hundreds of comments, but where it and the measurements disagree, the
measurements win — `docs/meals-deviations.md`, `docs/saed-v2-plan.md` and
`docs/meals-v2-findings.md` are what describe the system now.
`docs/working-agreement.md` is the shorter companion: the rules an agent changing this
repository works under, each with the measurement that earned it.

The specification lies in the repository as `docs/TZ_MEALS_v5.1.txt`. This is not
decoration: the code refers to it hundreds of times ("§2.7", "§4.3", "§8.2"), and all
sixty tunable numbers are explained only there. It was written in Russian and is kept
here in translation, with the section numbering, the formulas, the field names and
every constant untouched — so every "§4.3" in the code still lands where it did. The
Russian original is not lost: it is commit `8b2fb5a`, blob `2efe49c`, SHA-256
`95382c260963375919bff2d28e02bab72958d3e490e4fef6320a993d8b3d6ba6`
(`git show 8b2fb5a:docs/TZ_MEALS_v5.1.txt`), and it stays the authority if a
translated sentence ever reads two ways. The file is DELIBERATELY kept out of
`versioning.CONFIG_INPUTS` — otherwise a typo in the text of the specification would
change `config_version` and devalue a frozen calibration just as a change to a formula
would.

Running it by hand:

```bash
export TWELVEDATA_API_KEY=...   # the same key as the current monitoring uses
export FRED_API_KEY=...         # new, for the VIX series only
python -m meals.backfill        # the one-off load of history
python -m meals.audit           # the coverage table
python -m meals.sessions        # regenerate the NYSE schedule
python -m meals.corporate_actions  # rebuild the ex-dividend table
python -m meals.pipeline        # recompute per-asset metrics
python -m meals.cross_section   # recompute basket metrics
python -m meals.saed            # recompute single-asset events
python -m meals.cluster         # SI-Index, cluster events, the decision journal
python -m meals.export          # export events to JSON under the schema
python -m meals.truth           # §7 truth labels and the baseline
python -m meals.evaluate        # score the detector against them
python -m meals.calibrate       # §7 fit on train (writes calibration.json; never reads test)
python -m price_monitor.economic_calendar --rebuild   # rebuild the calendar archive
```

The order matters: `cross_section` works off the per-asset metrics, `saed` off the
basket factor, `cluster` off the basket metrics and the SAED residuals, and `export`
off all of it at once.

### Versions and reproducibility

Every decision is stamped with two versions. `config_version` is a hash of the content
of everything that affects the computation: the basket configuration and the thirteen
modules with the formulas and thresholds. Editing a threshold changes the version by
itself, with no manual counter that gets forgotten exactly when it matters most.
`run_version` is a hash of `config_version` and a fingerprint of the raw data (bars,
VIX, the schedule, ex-dates, the calendar archive). Derived files are not part of the
fingerprint: the basket metrics are both an input and an output of the `cluster` run,
and including them would mean a new version on every repeat.

Per §6.3 the stamp goes on everything the run writes, not only on the journal: the
per-asset metrics, the basket metrics, the cluster events and their escalations, the
SAED events and the block alerts. A reader holding one table can then state which
version produced it, which is what §6.3 requires before events of different versions
may be compared at all.

Cluster events carry three more fields. `created_at` is the moment the row FIRST
appeared, carried across runs rather than re-taken from the clock — otherwise a rerun
over unchanged data would differ byte for byte and the idempotency of §6.2 would not
exist. `recalculated` says that late or revised data reached this row, and it is judged
on `data_fingerprint` — the third field, the hash of the raw inputs — rather than on
`run_version`. `run_version` hashes the configuration together with the data, so it
moves on a code edit too, and a flag judged on it would stand at True on every row
throughout calibration, when thresholds move on every iteration. What the code changed
is already what `config_version` is for; see `docs/meals-deviations.md` §17.

SAED events carry `overlap_with_cluster` (§8.2). It is filled by the `cluster` run, not
the `saed` one: SAED runs first, so when its events are built the cluster events of this
run do not exist yet. Until then the field is NULL rather than False — under §1.2 that
is the difference between "no overlap" and "not evaluated". The span compared against is
in wall-clock hours even though the cooldown is counted in reference-calendar ones: a
crypto event can land on a Saturday, when the reference calendar has no hours at all,
and a cluster event opened on Friday is still active then. On the current history 1,494
of 2,478 single-asset events fall inside an active cluster event.

Two properties follow. A repeat run over unchanged data gives the same `run_version` and
a byte-identical result — verified over the whole history: 188 cluster events, 324,950
journal rows and 188 export files match between two runs. And a bar sent late or
corrected by the vendor changes the fingerprint, so the recomputation gets a new version
automatically.

Two directories are not kept in the repository: the event export `data/meals/events/`
(9.7 MB of JSON, rewritten in full on every run — `run_version` is in every file) and
the residual series `data/meals/residuals/` (32 MB). Both are fully derived from the
metrics and the code and are restored in seconds: `python -m meals.saed` and
`python -m meals.export`. The per-asset metrics stay in the repository — they depend
only on the bars and cost a full `pipeline` run. The export format is fixed by the
schema `schema/event_export.schema.json`, and a test validates against it both a
synthetic event and all 188 real ones.

The archive of economic events is assembled entirely from ForexFactory's monthly pages,
from a single source and with no key. The live weekly feed extends it forward on every
Saturday digest, and the actual for released events is read back from the same place
month by month. Why there is one source and what was tried before it is above, in the
calendar section, and in `docs/meals-deviations.md`.

### What actually reaches you, and the switch that stops it

Three channels feed one delivery stream. They differ in what they measure, not in how
important they are — severity is a separate axis, and it is the same ladder for all
three.

| channel | asks | example |
|---|---|---|
| abnormal | was this move unexplained by the market | *biggest unexplained move in about a year* |
| absolute | was this simply a big move for this instrument | *biggest move in about three years* |
| market | was the market as a whole disorderly | *most disorderly hour in about two months* |

Severity is a **return period** — how long you would ordinarily wait to see something
this large in this instrument — because "the biggest move in Bitcoin since March 2023"
needs no calibration intuition where a 1-to-100 score would. Four rungs: a fortnight,
two months, a year, three years. Fitted per instrument on its own history, so a
once-a-year move in `SHY` and one in `SOL` mean the same thing to a reader while being
wildly different percentages.

Delivery splits by urgency, not by importance:

* **Pushed at once** — the rarest tier. Roughly one every three to four weeks.
* **Pushed after six bars** — a once-a-year move, and only if it is still standing.
  Of the events still standing at six bars, 72% were still standing at twenty-four,
  against 32% of those that had already given it back.
* **Tuesday and Friday digest** — everything else that held, about two items a note.
  Tuesday covers the weekend and Monday, when crypto trades straight through and
  equities gap on the open; Friday closes the trading week. This is *separate from the
  Saturday calendar digest on purpose*: that one is a forecast of what is scheduled,
  this one is a report of what happened, and reading them as one message makes both
  harder to skim.
* **Dropped** — the move reverted. Not a failure of the detector: it correctly found an
  unusual move, and then the move gave itself back. About two in five.

`price_monitor/meals_delivery.py` renders and sends these; it decides nothing, because
the channel and the digest slot are already stamped on each event by `meals.routing`.
It rides the same hourly trigger as everything else rather than taking a schedule of
its own, and nothing older than 48 hours is ever sent — without that rule the first
run would deliver five years of history at once.

**It is silent by default.** `meals_alerts_muted: true` in `config/config.yaml`, and
that is where it lives rather than on the scheduler's side, for the same reason
`alerts_muted` does: *"we are deliberately silent"* is a state of the project and has
to be visible where the code is. A cron job switched off on someone else's website
looks like a breakage a month later and there is nobody left to tell which it was. Set
it to `false` to start receiving messages; nothing else needs changing.

The NYSE schedule is built by the `exchange_calendars` library, but not on every run:
the library works as a generator, and its result lies in the repository as a table. That
way a repeat run over the same period gives the same answer even if the library has been
updated in the meantime, and the hourly monitoring does not need the calendar library at
all — it reads a ready-made CSV. The table is filled to the end of 2028; when it nears
its end, the command has to be run again.


## Running locally

```bash
pip install -r requirements-dev.txt
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export TWELVEDATA_API_KEY=...  # needed for the currency pairs, see "Quick start"
python -m price_monitor
```

The tests are run with `pytest -q`.

## Limitations

- `lookback`/`mad_window` (300/288 candles at present) is not an API limit (Yahoo
  serves 60+ days per request, and `coinbase.py` can paginate past the limit of 300)
  but a deliberate choice: a backtest with a base of up to 700 candles gave no gain
  either in frequency or in recall.
- `price-monitor.yml` has no schedule of its own — without an external alarm clock (see
  "A reliable hourly trigger" above) the monitoring will not start by itself, only
  manually through Run workflow.
- There are exactly 8 currency pairs — that is Twelve Data's free limit (8 requests a
  minute). Adding a ninth without removing one of the current ones is impossible without
  a paid plan.
