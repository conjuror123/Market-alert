"""Run health for the report card: how often the hourly job actually ran.

    PYTHONPATH=. python tools/run_health.py RUNS.json [RUNS.json ...] OUT.json

where each RUNS.json is a page of the GitHub Actions API's workflow-runs
response for price-monitor.yml. Feed OUT.json to tools/dashboard.py --ops.

WHAT "LOST" MEANS HERE, AND WHY IT IS NOT "FAILED". The obvious measure is the
share of the day's runs that ended in failure, and it is worthless for this
system: the worst outage in the record - six days in early September 2026, when
the schedule trigger stopped - produced no failed runs at all, because it
produced no runs at all. By that measure those six days score a perfect zero.

So a day is scored by how many of its 24 hourly slots carried a SUCCESSFUL run.
A slot is lost whether the run failed, was killed on the twenty-minute timeout,
or never started. That is the number a reader of the page cares about, because
it is the number of hours in which nothing could have reached their phone.

PARTIAL DAYS ARE DROPPED, not scored. The first day in the window usually begins
mid-morning because that is where the API page happens to start, and the last is
still running. Scored against 24 slots they read as outages - the first cut of
this measurement had 28 August at 88% lost, which is not a fact about the system
but a fact about where I started counting. The last day is kept, scored against
the hours that have actually elapsed, because a reader wants to know about today.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from datetime import datetime, timedelta, timezone

# The hour the basket moved off one provider and onto two, splitting the feed:
# commit 4b0a446, "Fetch the basket from the provider that prices it best".
# Durations either side of it are not measuring the same job.
FEED_CHANGE = datetime(2026, 9, 14, 23, 20, tzinfo=timezone.utc)
SLOTS_A_DAY = 24


def moment(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load(paths) -> list:
    runs, seen = [], set()
    for path in paths:
        blob = json.load(open(path, encoding="utf-8"))
        for run in blob.get("workflow_runs", blob if isinstance(blob, list) else []):
            if run["id"] not in seen:
                seen.add(run["id"])
                runs.append(run)
    return sorted(runs, key=lambda r: r["run_started_at"])


def durations(runs) -> dict:
    """Median and p90 seconds of the runs that succeeded, and how many there were."""
    ok = sorted((moment(r["updated_at"]) - moment(r["run_started_at"])).total_seconds()
                for r in runs if r["conclusion"] == "success")
    if not ok:
        return {"n": len(runs), "median": 0, "p90": 0}
    return {"n": len(runs), "median": round(st.median(ok)),
            "p90": round(ok[min(len(ok) - 1, int(0.9 * len(ok)))])}


def by_day(runs) -> list:
    """Share of each day's hourly slots that carried no successful run."""
    covered: dict = {}
    for run in runs:
        started = moment(run["run_started_at"])
        covered.setdefault(started.date(), set())
        if run["conclusion"] == "success":
            covered[started.date()].add(started.hour)

    first, last = moment(runs[0]["run_started_at"]), moment(runs[-1]["run_started_at"])
    out, day = [], first.date()
    while day <= last.date():
        hours = covered.get(day, set())
        if day == first.date() and first.hour > 0:
            day += timedelta(days=1)         # the window starts mid-day; not an outage
            continue
        slots = last.hour + 1 if day == last.date() else SLOTS_A_DAY
        out.append({"day": day.isoformat(),
                    "lost_pct": round(100 * (slots - len(hours)) / slots)})
        day += timedelta(days=1)
    return out


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    *sources, destination = argv
    runs = [r for r in load(sources) if r["status"] == "completed"]
    if not runs:
        print("no completed runs in those files", file=sys.stderr)
        return 1

    ops = {"runs": len(runs),
           "before": durations([r for r in runs
                                if moment(r["run_started_at"]) < FEED_CHANGE]),
           "after": durations([r for r in runs
                               if moment(r["run_started_at"]) >= FEED_CHANGE]),
           "days": by_day(runs)}
    json.dump(ops, open(destination, "w", encoding="utf-8"), indent=1)
    worst = max(ops["days"], key=lambda d: d["lost_pct"])
    print(f"{ops['runs']} runs over {len(ops['days'])} whole days, "
          f"{moment(runs[0]['run_started_at']):%Y-%m-%d} to "
          f"{moment(runs[-1]['run_started_at']):%Y-%m-%d}; "
          f"worst day {worst['day']} at {worst['lost_pct']}% lost; "
          f"median run {ops['before']['median']}s before the feed change, "
          f"{ops['after']['median']}s after -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
