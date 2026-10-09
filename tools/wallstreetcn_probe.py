"""One-off: does Wallstreetcn answer where the bot runs? Its hourly bars of the
LME's three metals, ICE cocoa and ICE cotton, saved for a measurement.

    python -m tools.wallstreetcn_probe OUT

Eastmoney answered the development machine's quote server but refused a GitHub
runner's every bar request; this asks Wallstreetcn's (api-ddc-wscn.awtmt.com)
from a runner. Each code's answer is written whole to OUT/<code>.json, and
OUT/summary.txt says what came back. The bot never runs this: it writes
nothing to the store.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

URL = "https://api-ddc-wscn.awtmt.com/market/kline"
# The LME's three-month tin, nickel and aluminium, ICE cocoa and cotton.
CODES = ("UKSN.OTC", "UKNI.OTC", "UKAH.OTC", "USCC.OTC", "USCT.OTC")
# The most it serves in one answer: 4,000 asked, about 2,000 hourly bars back.
TICKS = 4000


def lines(payload: dict, code: str) -> list:
    """Its bars as served: [open, close, high, low, start in UTC seconds]."""
    return ((((payload or {}).get("data") or {}).get("candle") or {}).get(code) or {}).get("lines") or []


def probe(out_dir: str, session) -> "list[str]":
    os.makedirs(out_dir, exist_ok=True)
    summary = []
    for code in CODES:
        try:
            resp = session.get(URL, params={"prod_code": code, "tick_count": TICKS, "period_type": 3600,
                                            "fields": "tick_at,open_px,close_px,high_px,low_px"},
                               headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            payload = resp.json()
        except Exception as exc:
            summary.append(f"{code}: failed - {str(exc)[:160]}")
            continue
        got = lines(payload, code)
        span = (f"{datetime.fromtimestamp(got[0][-1], timezone.utc):%Y-%m-%d %H:%M} .. "
                f"{datetime.fromtimestamp(got[-1][-1], timezone.utc):%Y-%m-%d %H:%M} UTC" if got else "")
        summary.append(f"{code}: HTTP {resp.status_code}, {len(got)} hourly bars {span}")
        with open(os.path.join(out_dir, f"{code}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.writelines(f"{line}\n" for line in summary)
    return summary


def main(argv: "list[str] | None" = None) -> int:
    import requests

    parser = argparse.ArgumentParser(description="Wallstreetcn's bars, asked from a runner")
    parser.add_argument("out_dir")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for line in probe(args.out_dir, requests.Session()):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
