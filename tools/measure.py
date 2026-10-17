"""The measurement behind the dashboard: what the code sends, measured afresh each run.

    PYTHONPATH=. python -m tools.measure OUT.json [--voters DIR] [--bars-dir DIR]

Everything is computed by the bot's own code, cold, from the bar store: the metrics by
jump.pipeline into a temporary directory, the readings and their 24-hour events by
jump.jumps.run, a push by routing.PUSH_TIERS, a vote by jump.verify (candidates,
judge_found, as the live pass votes). Nothing is written to the store, the record or the
metrics the bot keeps, and nothing is kept between runs.

THE VOTES ARE TAKEN AGAIN, by this code, as the live pass would take them now: each
instrument's far moves inside its recount window (verify.recount_days: 9 days for the funds
and pairs, 29 for the coins, 79 for the LME; past it the bot never votes again), put to
its sources on their bars as tools/voter_bars.py --recount fetched them (`--voters`), and
voted again with the record so far until it settles, as the bot's record does over its
runs. Nothing older is voted: the bot votes a move only while its sources still serve it.

TWO READINGS. "The detector alone": every reading, no vote - the two years and the archive.
"With the votes": the votes taken here applied as the live run applies its record (a move
voted not real left out, a first hour voted overnight moved into its night). The votes
change only the readings inside their windows, so that is where the two are compared.

WINDOWS. The year to the measurement (the voters' fetch, or now without them) and the year
before it; the archive by calendar year. An event belongs to the window its first reading
was found in (`event_start`).

SELF-CHECKS. The counts are cross-added - blocks to the basket, instruments to their block,
words to the total, years to the archive, windows and verdicts to the votes, each source's
answers to its asks, the instruments to the votes' effect - every vote must lie inside its
instrument's window, the votes must change nothing outside the windows, and every
instrument must have scored. A check that fails makes the command fail, so no wrong page
is published.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from jump import jumps, routing
from jump.basket import load_basket
from tools.stage_report import BIGGEST_SHARE

log = logging.getLogger("tools.measure")

DAY = 86400
# A store whose newest bar is this much older than the measurement is named on the page:
# its newest hours were not in what this run had (a market holiday does it too).
BEHIND_DAYS = 4


def windows(end: int) -> "list[dict]":
    """The 365 days to `end`, and the 365 before them."""
    out = []
    for back in (1, 2):
        start, stop = end - back * 365 * DAY, end - (back - 1) * 365 * DAY
        out.append({"name": f"year to {datetime.fromtimestamp(stop, timezone.utc):%Y-%m-%d %H:%M}",
                    "start": start, "end": stop, "weeks": 365 / 7})
    return out


def event_table(readings: pd.DataFrame) -> pd.DataFrame:
    """One row per 24-hour event (jumps.events): its biggest reading, worded by it,
    pushed or a row in the weekly note."""
    if readings.empty:
        return pd.DataFrame(columns=["ticker", "block", "event_start", "hour_utc", "word",
                                     "push", "reading", "z", "r"])
    events = jumps.events(readings)
    events = events.assign(push=events["word"].isin(routing.PUSH_TIERS).to_numpy(dtype=bool),
                           event_start=events["event_start"].astype("int64"))
    return events[["ticker", "block", "event_start", "hour_utc", "word", "push", "reading",
                   "z", "r"]].reset_index(drop=True)


def _inside(events: pd.DataFrame, window: dict) -> pd.DataFrame:
    return events[(events["event_start"] >= window["start"])
                  & (events["event_start"] < window["end"])]


def volume(events: pd.DataFrame, window: dict) -> dict:
    """Pushes and note rows in one window: counts, a week, the busiest week, by word."""
    inside = _inside(events, window)
    pushes, rows = int(inside["push"].sum()), int((~inside["push"]).sum())
    weeks = (pd.to_datetime(inside.loc[inside["push"], "event_start"], unit="s")
             .dt.to_period("W-FRI").value_counts())
    busiest = {"pushes": int(weeks.max()), "week_to": str(weeks.idxmax().end_time.date())} \
        if len(weeks) else {"pushes": 0, "week_to": None}
    return {"pushes": pushes, "rows": rows,
            "pushes_a_week": round(pushes / window["weeks"], 2),
            "rows_a_week": round(rows / window["weeks"], 2),
            "busiest_week": busiest,
            "by_word": {w: int((inside["word"] == w).sum()) for w in jumps.WORDS}}


def split(events: pd.DataFrame, window: dict, key: str) -> "dict[str, dict]":
    """Pushes and rows in one window, by `key` (block or ticker)."""
    out = {}
    for name, rows in _inside(events, window).groupby(key):
        out[str(name)] = {"pushes": int(rows["push"].sum()), "rows": int((~rows["push"]).sum())}
    return out


def coverage(basket, metrics_dir: str) -> "list[dict]":
    """Each instrument's scored history: where it comes from, first and last hour, hours."""
    out = []
    for asset in basket.instruments:
        row = {"ticker": asset.ticker, "label": asset.label, "block": asset.block,
               "display": asset.display, "provider": asset.fetched_from,
               "first": None, "last": None, "hours": 0}
        path = os.path.join(metrics_dir, f"{asset.file_stem}.parquet")
        if os.path.exists(path):
            hours = pd.read_parquet(path, columns=["hour_utc"])["hour_utc"].astype("int64")
            row.update(first=int(hours.min()), last=int(hours.max()), hours=int(len(hours)))
        out.append(row)
    return out


def instrument_years(cover: "list[dict]", year: int, end: int) -> float:
    """Years of history the basket's instruments had in a calendar year, up to `end`."""
    start = int(pd.Timestamp(year, 1, 1, tz="UTC").timestamp())
    stop = min(int(pd.Timestamp(year + 1, 1, 1, tz="UTC").timestamp()), end)
    total = 0.0
    for c in cover:
        if c["first"] is None:
            continue
        overlap = min(stop, c["last"] + 3600) - max(start, c["first"])
        total += max(overlap, 0) / (365.25 * DAY)
    return round(total, 2)


def archive(events: pd.DataFrame, cover: "list[dict]", end: int) -> "list[dict]":
    """Events and pushes by calendar year since the first bar, and the instrument-years
    they came from."""
    first = min(c["first"] for c in cover if c["first"] is not None)
    out = []
    for year in range(datetime.fromtimestamp(first, timezone.utc).year,
                      datetime.fromtimestamp(end - 1, timezone.utc).year + 1):
        start = int(pd.Timestamp(year, 1, 1, tz="UTC").timestamp())
        full = int(pd.Timestamp(year + 1, 1, 1, tz="UTC").timestamp())
        stop = min(full, end)
        mine = events[(events["event_start"] >= start) & (events["event_start"] < stop)]
        out.append({"year": year, "partial": stop < full,
                    "instrument_years": instrument_years(cover, year, end),
                    "events": int(len(mine)), "pushes": int(mine["push"].sum())})
    return out


def biggest_hours(basket, metrics_dir: str, end: int) -> dict:
    """Each instrument's top 0.01% of settled hours by size of move (tools/stage_report's
    measure), and the word the detector gave each - the hours nobody would want missed."""
    window, ladder = jumps.settings()
    counts = {w: 0 for w in jumps.WORDS} | {"not flagged": 0}
    per = []
    for asset in basket.instruments:
        path = os.path.join(metrics_dir, f"{asset.file_stem}.parquet")
        if not os.path.exists(path):
            continue
        m = pd.read_parquet(path, columns=["hour_utc", "r", "gap"])
        s = jumps.score(m, asset.session_template, window, ladder)
        s = s[~s["young"].astype(bool) & np.isfinite(s["r"]) & (s["hour_utc"] < end)]
        if s.empty:
            continue
        size = s["r"].abs()
        big = s[size >= size.quantile(1 - BIGGEST_SHARE)]
        words = big["word"].fillna("not flagged").astype(str)
        for w, n in words.value_counts().items():
            counts[w] += int(n)
        per.append({"ticker": asset.ticker, "hours": int(len(big)),
                    "not_flagged": int((words == "not flagged").sum())})
    return {"by_word": counts, "total": int(sum(counts.values())), "by_instrument": per}


# --- the votes, taken again ---------------------------------------------------------

def read_fetch(voters_dir: str) -> dict:
    """tools/voter_bars.py's first line (when it fetched, how far back) and its failures."""
    with open(os.path.join(voters_dir, "failed.txt"), encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f]
    head, _, span = lines[0].partition(", ")   # fetched YYYY-MM-DD HH:MM UTC, <how far>
    words = head.split()
    at = datetime.strptime(f"{words[1]} {words[2]}", "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc)
    return {"at": int(at.timestamp()), "span": span,
            "failed": [line for line in lines[1:] if line.strip()]}


def _z_of(asset, metrics_dir: str) -> "dict[tuple[int, str], float]":
    """Each reading's z, as the detector scores it, by (hour, the vote's check)."""
    from jump import verify

    window, ladder = jumps.settings()
    path = os.path.join(metrics_dir, f"{asset.file_stem}.parquet")
    if not os.path.exists(path):        # its metrics failed: named by the checks
        return {}
    m = pd.read_parquet(path, columns=["hour_utc", "r", "gap"])
    parts = [jumps.score(m, asset.session_template, window, ladder),
             jumps.score_gaps(m, window, ladder, asset.session_template)]
    s = pd.concat([p for p in parts if not p.empty], ignore_index=True)
    return {(int(h), verify.CLOSE if k == jumps.HOUR else verify.OPEN): float(z)
            for h, k, z in zip(s["hour_utc"], s["reading"], s["z"])}


def vote_windows(basket) -> "dict[str, float]":
    """Each voted instrument's recount window in days (verify.recount_days): how long
    the live pass votes a move, while the closest-reaching of its sources serves it."""
    from jump import verify

    return {a.ticker: float(verify.recount_days(a)) for a in basket.instruments
            if verify.sources_for(a)}


def take_votes(basket, bars_dir: str, metrics_dir: str, voters_dir: str,
               now: int) -> "tuple[list[dict], dict]":
    """The votes the live pass would hold now: each instrument's far moves inside its
    recount window, voted by this code on the voters' saved bars (verify.candidates,
    verify.judge_found), then voted again with that record - a move voted not real leaves
    the yardstick, and one far only without it is a candidate too - until the verdicts
    settle, as they do over the bot's runs. Returns one row per vote and the record they
    make, in the bot's own format."""
    from jump import bars, corporate_actions, sessions, verify

    settings = jumps.settings()
    table = sessions.load_sessions()
    dividends = corporate_actions.load_dividends()
    rows, record = [], {}
    for asset in basket.instruments:
        sources = verify.sources_for(asset)
        if not sources:
            continue
        days = float(verify.recount_days(asset))
        since = now - int(days * DAY)
        frame = bars.load(bars.store_path(bars_dir, asset.file_stem),
                          since - verify.tail_days(settings[0]) * DAY)
        if frame.empty:
            continue
        asked = []
        for src in sources:
            path = os.path.join(voters_dir, src.name, f"{asset.file_stem}.parquet")
            asked.append((src.name, verify._priced(pd.read_parquet(path))
                          if os.path.exists(path) else None))
        mine: "dict[tuple, dict]" = {}
        judged: list = []
        for _ in range(4):
            found = verify.candidates(asset, frame, table, now, since, basket, dividends,
                                      settings, mine)
            judged = verify.judge_found(asset, frame, found, sources, asked, now)
            again = {(asset.asset_id, c["hour"], c["check"]): verify._row(
                         asset, c, verdict, stored, names, moves, seen, now)
                     for c, (verdict, stored, names, moves, seen) in judged}
            settled = ({k: r["verdict"] for k, r in again.items()}
                       == {k: r["verdict"] for k, r in mine.items()})
            mine = again
            if settled or not any(r["verdict"] in verify.KEPT for r in again.values()):
                break
        record.update(mine)
        if not judged:
            continue
        z = _z_of(asset, metrics_dir)
        for c, (verdict, stored, names, moves, seen) in judged:
            rows.append({"ticker": asset.ticker, "block": asset.block, "hour": int(c["hour"]),
                         "check": c["check"], "z": z.get((int(c["hour"]), c["check"])),
                         "stored": float(stored), "verdict": verdict, "window_days": days,
                         "age_days": round((now - int(c["hour"])) / DAY, 2),
                         "answers": [{"source": n, "move": None if m is None else float(m),
                                      "saw": n in seen} for n, m in zip(names, moves)]})
    return rows, record


def write_record(record: dict, path: str) -> None:
    """The votes as a record the detector reads (jump.verify.load)."""
    from jump import verify

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=verify.COLUMNS, lineterminator="\n",
                                extrasaction="ignore")
        writer.writeheader()
        for row in sorted(record.values(), key=lambda r: (r["asset_id"], int(r["hour_utc"]),
                                                          r["check"])):
            writer.writerow(row)


VERDICTS = ("real", "uncertain", "not_real", "overnight")


def vote_tables(votes: "list[dict]", bottom: float, windows_of: "dict[str, float]") -> dict:
    """The votes counted: by verdict (all, and those far enough to be flagged), by
    recount window, by source, by block."""
    def count(rows):
        return {v: sum(r["verdict"] == v for r in rows) for v in VERDICTS}

    flagged = [r for r in votes if r["z"] is not None and abs(r["z"]) >= bottom]
    by_window = []
    for days in sorted(set(windows_of.values())):
        mine = [r for r in votes if windows_of.get(r["ticker"]) == days]
        answered = [sum(a["move"] is not None for a in r["answers"]) for r in mine]
        by_window.append({"days": days,
                          "instruments": sorted(t for t, d in windows_of.items() if d == days),
                          "votes": len(mine), **count(mine),
                          "answers_a_vote": round(float(np.mean(answered)), 2) if answered
                          else None})
    sources: "dict[str, dict]" = {}
    for r in votes:
        for a in r["answers"]:
            s = sources.setdefault(a["source"], {"asked": 0, "saw": 0, "did_not": 0, "outage": 0})
            s["asked"] += 1
            if a["move"] is None:
                s["outage"] += 1
            elif a["saw"]:
                s["saw"] += 1
            else:
                s["did_not"] += 1
    blocks = {}
    for b in sorted({r["block"] for r in votes}):
        blocks[b] = count([r for r in votes if r["block"] == b])
    return {"votes": len(votes), "by_verdict": count(votes),
            "flagged": len(flagged), "flagged_by_verdict": count(flagged),
            "by_window": by_window, "by_source": sources, "by_block": blocks}


def _in_windows(events: pd.DataFrame, windows_of: "dict[str, float]", end: int) -> pd.Series:
    """Whether each event began inside its instrument's recount window, or in the day
    before it - an event runs 24 hours from its first reading, so one begun then can hold
    a voted reading - and before `end`. The yardstick looks back only (jumps), so a vote
    changes no reading before its own hour."""
    start = end - (events["ticker"].map(windows_of).astype("float64") + 1) * DAY
    return (events["event_start"] >= start) & (events["event_start"] < end)


def effect(detector: pd.DataFrame, voted: pd.DataFrame, windows_of: "dict[str, float]",
           end: int) -> dict:
    """What the votes change: each voted instrument's pushes and note rows inside its
    recount window (_in_windows), the detector alone and with the votes; and both
    readings' events outside the windows, which the votes must leave as they are."""
    def counts(events):
        return {"pushes": int(events["push"].sum()), "rows": int((~events["push"]).sum())}

    d_in, v_in = _in_windows(detector, windows_of, end), _in_windows(voted, windows_of, end)
    per = {}
    for ticker, days in sorted(windows_of.items()):
        per[ticker] = {"window_days": days,
                       "detector": counts(detector[d_in & (detector["ticker"] == ticker)]),
                       "voted": counts(voted[v_in & (voted["ticker"] == ticker)])}
    return {"detector": counts(detector[d_in]), "voted": counts(voted[v_in]),
            "by_instrument": per,
            "outside": {"detector": counts(detector[~d_in & (detector["event_start"] < end)]),
                        "voted": counts(voted[~v_in & (voted["event_start"] < end)])}}


def behind(cover: "list[dict]", end: int) -> "list[dict]":
    """The instruments whose newest stored bar is BEHIND_DAYS or more before the
    measurement: their newest hours were not in the store this run had."""
    return [{"ticker": c["ticker"], "provider": c["provider"], "last": c["last"]}
            for c in cover if c["last"] is None or c["last"] < end - BEHIND_DAYS * DAY]


def not_carried(basket=None, bars_dir: "str | None" = None,
                keyed: "tuple[str, ...]" = ()) -> "list[str]":
    """The instruments the live run's open months do not carry (no open month in the
    store) whose own provider needs no key, or is one of `keyed`: the workflow brings them
    up to now with the bot's own fetch. The workflow names SiftingIO, for the two metals
    the live run does not fetch yet - a page each, two calls a code push (the user's,
    2026-10-12); Tiingo's and Twelve Data's quotas stay the live run's, and an instrument
    of theirs is left behind and named on the page."""
    from jump import backfill, bars

    basket = basket or load_basket()
    bars_dir = bars_dir or bars.DEFAULT_BARS_DIR
    out = []
    for asset in basket.instruments:
        store = bars.store_path(bars_dir, asset.file_stem)
        opened = os.path.isdir(store) and any(n.endswith(".open.csv") for n in os.listdir(store))
        free = asset.fetched_from not in backfill.PROVIDER_KEYS or asset.fetched_from in keyed
        if not opened and free:
            out.append(asset.ticker)
    return out


# --- the checks -------------------------------------------------------------------

def checks(result: dict) -> "list[dict]":
    """Cross-additions of the result: each a name, whether it held, and what it compared."""
    out = []

    def check(name, ok, detail):
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    missing = [c["ticker"] for c in result["coverage"] if c["first"] is None]
    check("every instrument scored", not missing and not result["meta"]["failures"],
          f"no metrics: {missing}; failures: {result['meta']['failures']}")
    for w in result["windows"]:
        total = result["volume"][w["name"]]
        blocks = result["by_block"][w["name"]]
        tickers = result["by_instrument"][w["name"]]
        for kind in ("pushes", "rows"):
            b = sum(x[kind] for x in blocks.values())
            t = sum(x[kind] for x in tickers.values())
            check(f"{w['name']}: {kind} of the blocks and instruments add up",
                  b == total[kind] == t, f"total {total[kind]}, blocks {b}, instruments {t}")
        words = sum(total["by_word"].values())
        check(f"{w['name']}: the words add up", words == total["pushes"] + total["rows"],
              f"words {words}, events {total['pushes'] + total['rows']}")
    years = sum(y["events"] for y in result["archive"])
    check("the archive's years add up", years == result["events"],
          f"years {years}, events {result['events']}")
    votes = result.get("votes")
    if votes:
        t = votes["tables"]
        wins = sum(w["votes"] for w in t["by_window"])
        check("the votes' windows add up", wins == t["votes"],
              f"windows {wins}, votes {t['votes']}")
        late = [f"{r['ticker']} {r['age_days']}d" for r in votes["list"]
                if r["age_days"] > r["window_days"]]
        check("every vote lies inside its instrument's recount window", not late,
              f"outside: {late[:10]}")
        verdicts = sum(t["by_verdict"].values())
        check("the votes' verdicts add up", verdicts == t["votes"],
              f"verdicts {verdicts}, votes {t['votes']}")
        bad = [n for n, s in t["by_source"].items()
               if s["asked"] != s["saw"] + s["did_not"] + s["outage"]]
        check("each source's answers add up to its asks", not bad, f"not adding up: {bad}")
        listed = len(votes["list"])
        check("every vote is listed", listed == t["votes"], f"listed {listed}, votes {t['votes']}")
        e = votes["effect"]
        for reading in ("detector", "voted"):
            for kind in ("pushes", "rows"):
                per = sum(x[reading][kind] for x in e["by_instrument"].values())
                check(f"the votes' effect, {reading}: {kind} of the instruments add up",
                      per == e[reading][kind], f"total {e[reading][kind]}, instruments {per}")
        check("the votes change nothing outside their windows",
              e["outside"]["detector"] == e["outside"]["voted"],
              f"detector {e['outside']['detector']}, with the votes {e['outside']['voted']}")
    return out


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def measure(bars_dir: str, voters_dir: "str | None", work_dir: str) -> dict:
    from jump import pipeline

    basket = load_basket()
    window_days, levels = jumps.settings()
    metrics_dir = os.path.join(work_dir, "metrics")
    failures: "list[tuple[str, str]]" = []
    pipeline.write_all(basket, bars_dir, metrics_dir, full=True, failures=failures)
    fetch = read_fetch(voters_dir) if voters_dir else None
    end = fetch["at"] // 3600 * 3600 if fetch else \
        int(datetime.now(timezone.utc).timestamp()) // 3600 * 3600
    wins = windows(end)
    no_record = os.path.join(work_dir, "no-record.csv")             # never written
    detector = event_table(jumps.run(metrics_dir, now=end, verified_path=no_record,
                                     failures=failures))
    votes = None
    if fetch:
        rows, record = take_votes(basket, bars_dir, metrics_dir, voters_dir, end)
        path = os.path.join(work_dir, "votes.csv")
        write_record(record, path)
        voted = event_table(jumps.run(metrics_dir, now=end, verified_path=path))
        windows_of = vote_windows(basket)
        votes = {"fetched": fetch, "list": rows,
                 "tables": vote_tables(rows, levels[0], windows_of),
                 "effect": effect(detector, voted, windows_of, end)}
    cover = coverage(basket, metrics_dir)
    result = {
        "meta": {"commit": os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD"),
                 "subject": _git("log", "-1", "--format=%s"),
                 "branch": os.environ.get("GITHUB_REF_NAME") or _git("branch", "--show-current"),
                 "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                 "end": end, "instruments": len(basket.instruments),
                 "levels": list(levels), "window_days": window_days,
                 "push_words": list(routing.PUSH_TIERS),
                 "last_bar": max((c["last"] for c in cover if c["last"] is not None),
                                 default=None),
                 "behind": behind(cover, end),
                 "failures": [f"{a}: {e}" for a, e in failures]},
        "windows": wins,
        "events": int(len(detector[detector["event_start"] < end])),
        "volume": {w["name"]: volume(detector, w) for w in wins},
        "by_block": {w["name"]: split(detector, w, "block") for w in wins},
        "by_instrument": {w["name"]: split(detector, w, "ticker") for w in wins},
        "coverage": cover,
        "archive": archive(detector[detector["event_start"] < end], cover, end),
        "biggest_hours": biggest_hours(basket, metrics_dir, end),
        "votes": votes,
    }
    result["checks"] = checks(result)
    return result


def provider_limits(usage_dir: str, end: int, days: int = 30) -> "list[dict]":
    """Each keyed provider's limit per window (jump.usage.limits) against what was
    asked (the usage files): the last `days` days to `end` for a day's limit, the
    last two months for a month's. An hour's or a minute's limit is the provider's
    own 429; it is shown with its shares only."""
    from jump import usage as u

    table = u.read_all(usage_dir)
    last = datetime.fromtimestamp(end, timezone.utc).date()
    out = []
    for row in u.limits().itertuples():
        mine = table[table["provider"] == row.provider] if not table.empty else table
        if row.window == "day":
            periods = [(last - timedelta(days=k)).isoformat() for k in range(days - 1, -1, -1)]
            of = lambda p: mine[mine["day"] == p]          # noqa: E731
        elif row.window == "month":
            first = last.replace(day=1)
            periods = [(first - timedelta(days=1)).strftime("%Y-%m"), first.strftime("%Y-%m")]
            of = lambda p: mine[mine["day"].str[:7] == p]   # noqa: E731
        else:
            periods, of = [], None
        series = [{"period": p, "calls": int(of(p)["calls"].sum()),
                   "by_purpose": {k: int(v) for k, v in of(p).groupby("purpose")["calls"].sum().items()}}
                  for p in periods]
        counted = [x for x in series if x["calls"]]
        out.append({"provider": row.provider, "window": row.window, "limit": int(row.limit),
                    "live": int(row.live), "manual": int(row.manual),
                    "dashboard": int(row.dashboard), "votes": int(row.votes),
                    "note": "" if pd.isna(row.note) else str(row.note),
                    "series": series, "periods_counted": len(counted),
                    "latest": series[-1] if series else None,
                    "peak": max(counted, key=lambda x: x["calls"]) if counted else None})
    return out


def main(argv: "list[str] | None" = None) -> int:
    from jump import bars

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out")
    parser.add_argument("--bars-dir", default=bars.DEFAULT_BARS_DIR)
    parser.add_argument("--voters", default=None,
                        help="tools/voter_bars.py's output; without it, no votes")
    parser.add_argument("--usage", default=None,
                        help="the usage files (jump.usage); the checkout's by default")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    from jump import usage as u

    with tempfile.TemporaryDirectory() as tmp:
        result = measure(args.bars_dir, args.voters, tmp)
    result["limits"] = provider_limits(args.usage or u.USAGE_DIR, result["meta"]["end"])
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, default=int)
    failed = [c for c in result["checks"] if not c["ok"]]
    for c in failed:
        print(f"CHECK FAILED: {c['name']} ({c['detail']})")
    print(f"{len(result['checks']) - len(failed)} of {len(result['checks'])} checks held; "
          f"wrote {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
