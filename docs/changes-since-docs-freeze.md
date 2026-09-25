# Changes since the docs freeze

> **Holds** every change made to the system since the owner started editing the existing
> documents by hand (22–23 September 2026), in one place, so that nothing is lost while the other
> documents are being rewritten. Each entry says what changed, why, what it measured, and
> **which existing document it belongs in**.
> **Does not hold** anything that is already written into those documents. When an entry has
> been folded in, delete it here.
> **Add to it** every change from now on, until the freeze ends. The existing documents are not
> edited in the meantime.

Newest first.

---

## 8. The gap has its own events; it meets the hourly ones only at "one per day"

**Commit:** `c6498a7`. **Changes alerts:** which reading describes 25 days in 24 years; not which
events exist. **Replaces** the same-rung rule in entry 7.

**What.** The gap is one reading covering the whole closed period: the previous session's last
close to the next session's first price, the whole night, or for a currency the whole weekend.
It was already scored on a path of its own, with its own history, yardstick, rungs and "biggest
since" records. But at the event step it was written onto a copy of the first hour's row, and
the two competed for that row. Now:

- **Its events are its own**, built by the same one-event-per-day logic from the gap readings
  alone. To measure how much of the gap held by the close, it reads the hourly returns after
  it, and changes nothing in them.
- **The hourly events are built exactly as before the gap existed.** Nothing hourly reads the
  gap.
- **They meet in one place:** one event per instrument (and per block) per trading day. On a day
  both fired, the event keeps the gap's identity, since it is the day's first reading, so a push
  already sent is edited rather than repeated. It is described by the rarer of the two: the
  higher rung, or at the same rung the one further past that rung. That is the rule every later
  hour of a day already follows. It replaces entry 7's "bigger % move" tie rule.

**Measured** (cold pass, whole history):
- With the gap switched off: 9,118 events. With it on, 8,963 of those are identical in every
  descriptive column (tier, channel, digest slot, retention, record date), and the other 155 are
  on days that a rarer gap now describes. No hourly event is altered.
- Against the previous version: the same 9,690 events, no rung or channel changes. 25 days are
  described by the other reading: 24 same-rung days where the first hour went further past the
  rung than the gap, and 1 the other way. Overnight pushes 110 → 104.
- Pushes 56.5 a year, 94.4% reached, held at the next close 74.2%.

**Belongs in:**
- `architecture.md` "1. The return": the drafted text below now includes it.
- `CLAUDE.md` invariant 4 ("One event per instrument per trading day. Enforced in `saed`"): still
  true. It is enforced in `saed.build_events` within each path and in `saed.merge_days`
  across the two.

---

## 7. At the same rarity rung, the bigger move takes the morning (replaced by 8)

**Commit:** `8b3ca8d`. **Changes alerts:** yes, which bar describes an event; not which events exist.

**What.** The overnight gap used to take a morning's event only when it reached a strictly
higher rarity rung than the first hour. At the same rung the first hour kept the event, whatever
the sizes. Now a second rule applies: **at the same rung, the bigger price move takes it.** That
compares the gap (last close to first price) with the first hour's own move, in %. An exact tie
stays with the first hour. A higher rung still always wins. It applies to instruments and
blocks alike.

**Measured** (cold pass, whole history):
- Same events, same channels: 9,690 events, 1,315 pushes, 56.5 a year, 94.4% reached.
- 44 mornings (35 digest rows, 9 pushes) now describe the gap instead of the first hour. None
  went the other way.
- Held up at the next close: 74.2% → 73.8%, because those 44 are now measured from the previous
  close through the night.

**Belongs in:**
- `architecture.md` "1. The return": the drafted text below now includes it.
- `README.md`: the numbers table.

---

## 6. A gap is judged against gaps after the same kind of close

**Commits:** `1548418`, `0f43598`, `71a3440`. **Changes alerts:** yes.

**What.** A US fund's opening gap follows a weeknight, a weekend or a holiday, and all three
were scored against one "usual gap". A gap after a weekend is typically 1.17× a weeknight's
(from 0.88× for XLP to 1.81× for UNG). It isn't three nights' worth (1.7×): most of what moves
a price over a weekend is the same few headlines a weeknight has. With one yardstick, Mondays
fired too often and weeknights too rarely.

- Each morning now carries its kind: **weekend** (a Saturday lies between, so a long weekend
  counts too), or **night**, which includes the morning after a midweek holiday.
- The usual gap is the fund's recent level of gaps × this kind's long-run share of it, learned
  per fund over 500 sessions: the same idea as the time-of-day scale for the first hour. Until
  a fund has seen 100 weekends (about two years), its ratio is 1. Both readings use it: the gap
  against the usual gap of its kind, and the "compared with its block" residual against the
  usual residual of its kind.
- **A gap after a midweek holiday counts as a weeknight** (the Friday after Thanksgiving, a
  midweek Fourth of July): about 1% of mornings, two or three a year. That's too few to learn a
  yardstick of their own. On the weekend's yardstick they fired 3× their share. One missed
  session is closer to a night than to a weekend. (Briefly, in `0f43598`, they were left
  unscored instead. That lost real mornings such as 2014-11-28, when OPEC declined to cut and
  USO, BNO and the energy block pushed, so it was reverted.)
- Currency pairs: the gap is always the weekend, so the ratio is exactly 1 and nothing changed.
  Measured: identical FX gap events.
- It is still always computed over the full history. The gap pass takes 7–8 s instead of 6 s.
- **Messages** for a fund after a weekend: "−4.21% at the open after the weekend", "9.0× usual
  weekend gap". The record line stays "the biggest opening gap since…". The record is the last
  opening gap of any kind that was at least this unusual for its own kind, so it reaches back
  through the whole history, not only through other Mondays. The event table has a new column,
  `gap_kind`.

**Measured** (cold pass, whole history, US funds):

| gap events | before | after | share of mornings |
|---|---|---|---|
| after a weeknight | 382 (60%) | 500 (72%) | 78% |
| after a weekend | 217 (34%) | 162 (23%) | 21% |
| after a midweek holiday (now a weeknight) | 29 (4.5%) | 30 (4.3%) | 1% |
| pushes: weeknight / weekend / holiday | 44 / 50 / 3 | 57 / 39 / 4 | |

- The weekend share of gap events now matches its share of mornings. Holidays still fire about
  4× their share, being bigger than a single night. That was accepted in exchange for keeping
  them scored.
- Totals barely move: events 9,638 → 9,690, pushes 1,313 → 1,315 (56.4 → 56.5 a year).
- A few borderline rungs reshuffled. SPY's 2024-08-05 gap drops from major (push) to high
  (digest). XLK and QQQ still push that morning.
- Hourly events are untouched except on the mornings where the gap now takes or gives back the
  day's event.
- The 44 funds' learned weekend ratios end at a median 1.17, matching the direct measurement
  on the bars.

**Belongs in:**
- `architecture.md` "1. The return": the drafted text below now includes it.
- `decisions.md`: why holidays count as weeknights, and why the record line stays "opening gap".
- `README.md`: the numbers table.

---|---|---|---|
| after a weeknight | 382 (60%) | 497 (74%) | 78% |
| after a weekend | 217 (34%) | 160 (24%) | 21% |
| after a holiday | 29 (4.5%) | 20 (3%) | 1% |
| pushes: weeknight / weekend / holiday | 44 / 50 / 3 | 59 / 39 / 0 | |

- The weekend share of gap events now matches the weekend share of mornings. Holidays still fire
  about 3× their share, because they share the weekend's ratio and are bigger than weekends.
  That's the price of too few holidays to learn from.
- Totals barely move: events 9,638 → 9,675, pushes 1,313 → 1,314 (56.4 a year either way).
- Hourly events are untouched except on the mornings where the gap now takes or gives back the
  day's event: 61 first-hour events became gap events, 49 came back.
- SPY's learned ratio by year runs 1.07–1.49 (2008 low, 2020 high); the 44 funds end at a median
  1.17, matching the direct measurement on the bars.

**Belongs in:**
- `architecture.md` "1. The return": the drafted text below now includes it.
- `decisions.md`: why two kinds and not three, and why the record line stays "opening gap".
- `README.md`: the numbers table.

---

## 5. The "biggest since" lookup is kept between runs

**Commit:** `59c8e49`. **Changes alerts:** no. **Changes the stored files:** yes, a new one.

**What.** Every alert names the last time the instrument moved this much ("the biggest since
3 March 2020"). Finding that date used to mean re-reading six years of history on every hourly
run. That was the only reason a warm events run held six years on top of its warm-up.

The date only ever depends on a handful of past moves: the ones that nothing at least as big
has come after since. Over twenty years that is at most 23 moves per series. Each run now saves
that short list, as of a checkpoint 14 days behind the newest bar, to
`data/tremor/record_book.parquet`. The next run starts from the list and reads only its warm-up
plus the bars after the checkpoint.

- **What is published:** a warm run publishes the events after the checkpoint (14+ days). That
  is more than delivery ever reads back: 48 hours for a push and 10 days for a note. Rate lines
  ("about N times a year") read the separate full archive, as before.
- **When the list can't be used:** if there is no list, or it came from another configuration,
  the run goes cold (reads all of history) and writes a new one. That happens on a fresh
  runner, after a cache miss, and after a formula change.
- **A series the list has never seen** (a new block, say) makes the run throw the list away,
  so the next run goes cold and takes that series in with its whole history.
- **Storage:** the file is gitignored and saved in the GitHub Actions cache with the metrics.
  It is not counted as "changed" when deciding whether to upload the cache, because it's
  rewritten every run. An older list is still valid; it just makes the next slice a little
  longer.

**Measured.**
- A warm run reads 40% of all bars instead of 72%, and takes 48 s instead of 74 s (locally, 4
  cores).
- Started from a list written by a run seven days earlier, a warm run published the same 50
  events as a full cold run: identical tiers, channels, digest slots and record dates.
- One XLF `extreme` in that set names a record from 2011, three years before its own slice
  begins. Under the old six-year slice, 19 events lost their record date; now none do.

**Belongs in:**
- `architecture.md`: the paragraph about what is derived and how warm runs work.
- `operations.md`: what is cached; the first run after a change is cold.
- `CLAUDE.md`: the "Working locally" line on derived data, which should now list
  `record_book.parquet`.
- The comment above `windows.trusted_bars` is marked as history. No caller uses the six-year
  horizon for slicing any more.

---

## 4. A fund's hour is judged against the same hour's history

**Commit:** `2b166c2`. **Changes alerts:** yes.

**What.** The "unusual compared with its block" check (the abnormal channel) divided each hour
by a short-memory yardstick that is the same at every hour. A fund's opening half-hour naturally
moves about twice as much as midday, so it was judged against yesterday's quiet afternoon:

- the abnormal rung was crossed on 0.61% of 09:00 bars, against 0.14–0.23% of other hours;
- 36× for XLI and 29× for XLV;
- the "big move" check found 09:00 no busier than 10:00 (0.74% against 0.73%).

Each hour's usual size, relative to all hours, is now learned per fund and folded into the
divisor (`residuals.hour_scale`). The level still comes from every hour; only the shape (for
example, "the opening is usually 2× midday") is per hour.

- **Memory:** 500 sessions. The shape is steady for years (XLI's opening ran 2.0–2.8× every year
  since 2008), and the 500-session version wobbles least: 1.3% a month, against 4.0% for an
  86-session memory. All three memories tested predicted the next half-year equally well
  (a 14–15% miss).
- **Scope:** US funds only. FX has a busy hour too, but it is when US data is released, and
  there the busy hour is the news. The "big move" check is untouched.

**Measured (full rebuild):**
- 09:00 "unusual" crossings: 0.61% → 0.35%.
- Pushes: 61.9 → 56.4 a year, a third fewer of them peaking at 09:00.
- Still standing at the next close: 74.2% → 74.4%.
- The pushes removed held up 73% of the time, like the ones kept. They were big for an
  opening, not for that fund's openings.

**Cost.** The scale reaches 22,003 bars back, longer than any other warm-up for a fund. Before
change 5, that grew a fund's warm slice by 15,043 bars (to about 2008) and warm runs from 59 s
to 74 s. They still matched cold runs on every event and tier.

**Not fixed by it.** The opening still crosses about 2× as often as other hours. What is left
is more extreme outliers at the open, not a bigger typical size, and fixing it means per-hour
rungs. That belongs to the rung re-cut, which is deliberately last.

**Belongs in:**
- `architecture.md`: how the abnormal channel standardises.
- `decisions.md`: why the shape and not a seventh of the history; why 500; why not FX.
- `concerns-for-later.md`: the remaining 2× at the open.

---

## 3. Currency weekend gaps, and gaps only across a real close

**Commit:** `ae0f066`. **Changes alerts:** yes, slightly.

**What.**
- **FX weekend gaps.** A currency pair trades Sunday 17:00 to Friday 17:00 New York time, so it
  has one gap a week: the weekend. It is scored the same way as a fund's overnight gap (change
  1), but with no dividend rule, since currencies have no dividends. Messages say "at the weekly
  open" and "weekend gap".
- **A gap only counts when the previous stored bar really is the last one before the close.**
  The biggest FX "gap" in the record, 2015-01-04 on three pairs at once, was a hole in the data:
  the store has no bars on 2015-01-02, so the "previous close" was days old. For a fund this is
  checked against the NYSE calendar; with no calendar, no fund gap is scored. For a pair, the
  previous bar must be within 50 hours of the Sunday open.

**Measured.**
- The completeness check refuses 0.9% of US mornings, all of them holes in the store
  (2020-02-19 among them), and 9–26% of FX weekends where the Friday afternoon is missing.
- FX adds 14 events since 2012, one of them a push: USD/CAD on 2025-02-02, the Canada tariffs
  weekend. Pushes 62.1 → 62.0 a year.

**Belongs in:** `architecture.md`, next to change 1.

---

## 2. `tremor/gaps.py` counts towards `config_version`

**Commit:** `52a4a18`. It decides every gap-claimed event's tier, and every one of those events
carries the version stamp. **Belongs in:** `CLAUDE.md` invariant 10 and `docs/decisions.md`,
which list the modules that make up the version.

---

## 1. The overnight gap is scored as its own reading

**Commit:** `046e06d`. **Changes alerts:** yes. **Changes committed data:** yes.

**What.** A US fund's first bar was measured from its own opening price, so the jump from the
last close to the first price was thrown away. That jump holds a median 43% of day-to-day
variance across the US funds (25% for XLU, 70% for CPER). SPY's biggest opening gaps
(2020-03-16, 2008-10-24, 2015-08-24, 2024-08-05) were seen only from the first price onward.

- **The first hour is left exactly as it was.** The gap is a second reading on the same row.
  It's scored against that fund's own history of gaps (block, residual, peer comparison and
  rungs, the same as an hour) in `tremor/gaps.py`. It takes the morning's event only when it
  reaches a strictly higher rung than the first hour. One event per instrument per day still
  holds.
- **Why not fold it into the first hour:** the 09:00 bar already carried 31% of US-fund events,
  and any combined number would have reshaped the busiest hour.
- **It is always scored over the full history, never warm.** That's 185,000 sessions, about 5 s
  a run. So it can't disagree between warm and cold runs.
- **Memory in days, not bars.** It uses the VIX's daily setting. The funds' hourly bar counts,
  read as sessions, would have meant 14 years of mornings.
- **A gap is left unscored where it might not be the market:**
  - on a date whose dividends haven't been confirmed. An unknown payout reads as a gap the size
    of the dividend: about 18 false events a year, 7 of them BKLN's;
  - on a split, and on any gap that looks like a split ratio;
  - on the first bar of the record.
- **Morning dividend check.** Once a session, on the first run after 09:30 New York,
  `tremor.backfill` asks Yahoo (`events=div`, no key, about 44 requests) for payouts. It adds
  any new ones to `data/tremor/corporate_actions.csv` and moves each fund's date in the new
  `data/tremor/dividend_checks.csv`. A fund that fails keeps its old date and is asked again the
  next hour. If a fund is 5 or more days behind, one line a day goes to the health chat.
- **Both files are committed every run** (`price-monitor.yml`).
- **Messages:** "−4.21% at the open", "usual overnight gap", "the biggest opening gap since…",
  "block opening", "biggest gaps: …". Retention counts the night as the event's first interval.

**Measured.**
- With the gap switched off, all 9,593 events are identical to before in every column except
  the version stamps.
- With it on: pushes 58.1 → 62.1 a year; overnight events about 27 a year, 4.3 of them pushes.
- Retention: 74.3% → 74.3%.
- Yahoo found all 227 of the last year's recorded payouts on the right dates. Its first real run
  found 15 September payouts the table lacked (e.g. XLRE and XLU on 2026-09-21).

**Belongs in:**
- `architecture.md` "1. The return": text is drafted below.
- `decisions.md`: its entry "The gap channel, `r_gap` and `gap_masked` — deleted" is now only
  half true; the gap is back, scored this time.
- `operations.md`: the daily Yahoo requests, the two committed files, the health line, the
  1 October check.
- `README.md`: the numbers table.

---

## Current headline numbers

From `tools/dashboard.py` on a cold pass of the current code. The `README.md` table still shows
the "before" column.

| | before (freeze) | now |
|---|---|---|
| pushes | 1,352, 58.1 a year, on 36.6 days a year | 1,315, **56.5 a year**, on 33.2 days a year |
| reached you | 94.4% of 216 obvious hours, 12 silent | **94.4%** of 216, 12 silent |
| false alarms | 85 (0.007%) | 238 (0.020%) as printed; see below |
| held up at the next close | 74.3% | **74.2%** |

**The false-alarm figure is misread by the tool, not by the detector.** `tools/report_card.py`
judges an event by the size of the hour it sits on. An overnight-gap event sits on the first
bar, whose own in-hour move is often small, because the gap was the move. Most of the 238 are
exactly that: counted like-for-like (overnight events left out) it was 96 after the gap-kind
change, against 85 before the gap existed. The time-of-day change made no difference to it
(221 both before and after). The gap-kind change moved it to 234, all of it overnight events
(like-for-like 97 → 96); scoring holidays as weeknights brought it to 238. **Open:** teach
the report card to judge an overnight event by its gap against the usual gap.

---

## Text already drafted for the existing documents

**`architecture.md`, replacing "1. The return":**

> **1. The return — and the gap before it.** Each hourly bar is scored on the move *inside* it:
> the first bar of a session from its own opening price, every later bar from the close of the
> bar before. But markets close. A US fund stops at 16:00 and opens again at 09:30; a currency
> pair stops on Friday evening and opens on Sunday evening. Whatever happened in between (news,
> other markets trading, a Fed decision on a Sunday) shows up as a jump between the last price
> before the close and the first price after the open. That jump is the **gap**, and it is
> scored as a separate, second reading on the first bar, never mixed into the hour itself. Each
> instrument's gap is compared with *its own* usual gap. A quiet utilities fund and a copper
> fund have very different nights, and a currency's weekend is different again. The gap then
> goes through the same checks and rarity rungs as an hour. A fund's gap is compared with its
> usual gap after the *same kind of close*, a Monday with other Mondays, because a weekend's
> gap is typically about a sixth bigger than a weeknight's. A gap after a midweek holiday is
> judged as a weeknight's. The gap produces events of its own and the hours never see it. There
> is still only one event per instrument per day: on a day both the gap and an hour fire, the day
> is described by the rarer of the two, just as a later, rarer hour takes over from an earlier
> one. When it is the gap, the message says so ("−4.21% at the open", "at the open after the
> weekend", or "at the weekly open" for a currency). A gap is left unscored whenever it might not be
> a real market move: when the fund's dividends for that day have not been confirmed, on a
> stock split, or when the stored data is missing the last bar before the close. Crypto never
> closes, so it has no gap.

**`architecture.md`, the derived-data paragraph:**

> Only the bars are committed. Everything computed from them (metrics, residuals, the events
> table, the record book) is gitignored. The hourly job keeps the metrics and the record book
> in the Actions cache and extends them. A full rebuild from the bars happens only when that
> cache is missing or `config_version` has moved.

**`README.md` quick start, point 3** (as agreed earlier, no provider names):

> 3. **Settings → Secrets and variables → Actions → Secrets.** Minimum required:
>    `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, which say where pushes go.
>    `TELEGRAM_HEALTH_CHAT_ID` is optional and sends health and provider failures somewhere
>    other than the product chat. Every other secret is a data provider, and which ones you
>    need is decided by `config/basket.yaml`: each instrument names the `provider` that answers
>    for it. A missing key costs you those instruments and nothing else; the run prices what it
>    can reach and stays silent on the rest. **To add a provider of your own:** a client module
>    in `price_monitor/` exposing `fetch_full_history(...)` and reading its key from the
>    environment; the name added to `PROVIDERS` in `tremor/basket.py`; a branch in the dispatch
>    in `tremor/backfill.py`; and the secret passed through `env:` in the workflows that fetch.
>    Then set `provider:` on the instruments it serves. `source` is identity and is never
>    edited to follow a provider change.

---

## Found while reading, not yet fixed in the documents

- `architecture.md` line 28 says "**Six** commands"; `CLAUDE.md`, which it calls the only copy,
  lists **four**.
- `architecture.md`, `CLAUDE.md`, `decisions.md` and `operations.md` all say a rebuild takes
  "about two minutes". A cold run of all four stages is now about 2.5–3 minutes locally, and
  longer on GitHub's runners.
- `CLAUDE.md` says "~820 tests, about four minutes". It is now 885 tests, about six minutes.
- `CLAUDE.md`'s table of documents does not list this file, so an agent starting fresh will not
  know to read it.

## To watch

- **2026-10-01:** the monthly payers (HYG, JNK, BKLN, PFF, SHY and others) go ex-dividend. Check
  that Yahoo lists their payouts by the 10:05 run. If it doesn't, their gaps stay unscored that
  day rather than going out wrong, and the health line appears after 5 days.
- **The first hourly run after each of these changes is a full rebuild,** slow by design.
