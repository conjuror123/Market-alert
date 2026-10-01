"""What the RapidAPI key's Metal Sentinel subscription serves for nickel and
aluminium (and tin, which it is not documented to carry).

Metal Sentinel (metal-sentinel.p.rapidapi.com) is documented only as "/metal-quote"
plus historical series fed by Kitco; its parameters are not public without a
RapidAPI login. So this asks a handful of likely paths and prints every answer
raw - RapidAPI says plainly when a path does not exist - then, for whatever
answers, reads the nickel and aluminium quote a few times a minute apart to see
whether, and how often, the price moves.

Read-only; prints, writes nothing. About 25 requests of the free plan's 15,000
a month.
"""
from __future__ import annotations

import os
import sys
import time

import requests

HOST = "metal-sentinel.p.rapidapi.com"


def get(path: str, **params) -> "tuple[int, object, dict]":
    headers = {"X-RapidAPI-Key": os.environ["RAPIDAPI_KEY"], "X-RapidAPI-Host": HOST}
    try:
        r = requests.get(f"https://{HOST}{path}", params=params, headers=headers, timeout=30)
    except requests.RequestException as exc:
        return 0, f"request failed: {type(exc).__name__}", {}
    finally:
        time.sleep(0.5)
    limits = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}
    try:
        return r.status_code, r.json(), limits
    except ValueError:
        return r.status_code, r.text[:300], limits


def main() -> int:
    if not (os.environ.get("RAPIDAPI_KEY") or "").strip():
        print("RAPIDAPI_KEY is not set")
        return 1
    print("PATHS - what answers")
    tries = [("/metal-quote", {}), ("/metal-quote", {"metal": "nickel"}),
             ("/metal-quote", {"symbol": "nickel"}), ("/metal-quote", {"metal": "NI"}),
             ("/metals", {}), ("/supported-metals", {}), ("/metal-list", {}),
             ("/market-status", {}), ("/historical", {"metal": "nickel"}),
             ("/metal-history", {"metal": "nickel"}), ("/intraday", {"metal": "nickel"})]
    limits = {}
    for path, params in tries:
        code, body, limits = get(path, **params)
        print(f"   {path} {params}: HTTP {code} {str(body)[:500]}")
    print(f"   rate-limit headers: {limits or 'none'}\n")

    print("MOVEMENT - the nickel and aluminium quote, five times a minute apart")
    for i in range(5):
        for metal in ("nickel", "aluminum", "tin"):
            code, body, _ = get("/metal-quote", metal=metal)
            print(f"   {time.strftime('%H:%M:%S', time.gmtime())} {metal:9s} HTTP {code} "
                  f"{str(body)[:240]}")
        if i < 4:
            time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
