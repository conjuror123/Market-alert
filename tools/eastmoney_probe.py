"""One-off: does Eastmoney answer where the bot runs? Its hourly bars of the
LME's three metals, ICE cotton and a fund, saved for a measurement.

    python -m tools.eastmoney_probe OUT

From the development machine Eastmoney's bar servers (push2his and its kin)
drop the connection after a few seconds; this asks them from a GitHub runner.
Each code's first full answer, hourly and daily, is written whole to
OUT/<secid>_<klt>.json, and OUT/summary.txt says per host and code what came
back. The bot never runs this: it writes nothing to the store.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Eastmoney's market.code: the LME's three-month tin, nickel and aluminium
# ("综合锡03" and kin), ICE cotton's front month, and SPY on NYSE Arca.
CODES = {"109.LTNT": "LME tin 3M", "109.LNKT": "LME nickel 3M", "109.LALT": "LME aluminium 3M",
         "108.CT00Y": "ICE cotton", "107.SPY": "SPY"}
HOSTS = ("push2his.eastmoney.com", "33.push2his.eastmoney.com", "push2.eastmoney.com",
         "push2delay.eastmoney.com")
PATH = "/api/qt/stock/kline/get"
HOURLY, DAILY = 60, 101
BEIJING = timezone(timedelta(hours=8))


def params(secid: str, klt: int) -> dict:
    return {"secid": secid, "klt": klt, "fqt": 0, "beg": 0, "end": 20500101, "lmt": 10000,
            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "ut": "fa5fd1943c7b386f172d6893dbfba10b"}


def parse(payload: dict) -> "list[tuple[int, float, float, float, float]]":
    """(stamp in UTC seconds, open, high, low, close) per line. Eastmoney writes
    a bar as "YYYY-MM-DD HH:MM,open,close,high,low,volume,..." in Beijing time;
    the stamp is taken as written, whether it marks the bar's start or end is
    for the measurement to find."""
    out = []
    for line in ((payload or {}).get("data") or {}).get("klines") or []:
        when, o, c, h, low = line.split(",")[:5]
        fmt = "%Y-%m-%d %H:%M" if " " in when else "%Y-%m-%d"
        stamp = datetime.strptime(when, fmt).replace(tzinfo=BEIJING)
        out.append((int(stamp.timestamp()), float(o), float(h), float(low), float(c)))
    return out


def probe(out_dir: str, session) -> "list[str]":
    """Asks every host for every code, hourly then daily; saves each code's
    first answer with bars. Returns the summary, one line per request."""
    os.makedirs(out_dir, exist_ok=True)
    lines = []
    for secid, name in CODES.items():
        for klt in (HOURLY, DAILY):
            saved = False
            for host in HOSTS:
                try:
                    resp = session.get(f"https://{host}{PATH}", params=params(secid, klt),
                                       headers={"User-Agent": "Mozilla/5.0",
                                                "Referer": "https://quote.eastmoney.com/"},
                                       timeout=30)
                    payload = resp.json()
                    got = parse(payload)
                except Exception as exc:
                    lines.append(f"{host} {secid} klt={klt}: failed - {str(exc)[:160]}")
                    continue
                span = (f"{datetime.fromtimestamp(got[0][0], timezone.utc):%Y-%m-%d %H:%M} .. "
                        f"{datetime.fromtimestamp(got[-1][0], timezone.utc):%Y-%m-%d %H:%M} UTC"
                        if got else "")
                lines.append(f"{host} {secid} klt={klt} ({name}): HTTP {resp.status_code}, "
                             f"{len(got)} bars {span}")
                if got and not saved:
                    with open(os.path.join(out_dir, f"{secid}_{klt}.json"), "w",
                              encoding="utf-8") as f:
                        json.dump(payload, f)
                    saved = True
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.writelines(f"{line}\n" for line in lines)
    return lines


def main(argv: "list[str] | None" = None) -> int:
    import requests

    parser = argparse.ArgumentParser(description="Eastmoney's bars, asked from a runner")
    parser.add_argument("out_dir")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for line in probe(args.out_dir, requests.Session()):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
