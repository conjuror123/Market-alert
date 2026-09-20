"""The four sample messages on the report card, rendered by the live delivery code.

Not mock-ups, and not hand-written: the page's whole claim is that its messages
are the ones the bot sends, so they are produced by price_monitor.tremor_delivery
from real rows in the events table the rest of the page is measured from.

    PYTHONPATH=. python tools/messages.py EVENTS.parquet OUT.json

where EVENTS.parquet came from `tremor.saed --full`. Feed OUT.json to
tools/dashboard.py --messages.
"""
import json, sys
from datetime import datetime, timezone
import pandas as pd
from price_monitor import tremor_delivery as md
from tremor.basket import load_basket

events = pd.read_parquet(sys.argv[1])
basket = load_basket()
labels = {a.asset_id: a.label for a in basket.instruments}
hist = events.to_dict("records")

def pick(**q):
    f = events
    for k, v in q.items():
        f = f[f[k] == v]
    f = f[~f["asset_id"].astype(str).str.startswith("block:")]
    return f.sort_values("hour_utc").iloc[-1].to_dict() if len(f) else None

wanted = [("extreme", "absolute", "The rarest thing it sends, and the loudest: a move "
           "large in raw size, on the rarest rung of the ladder."),
          ("major", "abnormal", "Found the other way — not big in itself, but bigger "
           "than the rest of its sector could account for."),
          ("high", "both", "Found by both routes at once. These hold up best: 80% are "
           "still standing at the next day's close."),
          ("noticeable", "absolute", "The common case, and it does not interrupt you — "
           "it is written into the running note instead.")]
out = []
for tier, basis, note in wanted:
    row = pick(tier=tier, basis=basis)
    if row is None:
        continue
    when = datetime.fromtimestamp(int(row["hour_utc"]), tz=timezone.utc)
    out.append({"tier": tier, "basis": basis, "note": note,
                "when": when.strftime("%d %B %Y, %H:%M UTC"),
                "text": md.format_push(row, labels, None, events=hist,
                                       now=when, rate_history=hist)})
json.dump(out, open(sys.argv[2], "w"), indent=1)
print(f"{len(out)} messages ->", sys.argv[2])
for m in out:
    print("\n---", m["tier"], m["basis"], m["when"], "---")
    print(m["text"][:400])
