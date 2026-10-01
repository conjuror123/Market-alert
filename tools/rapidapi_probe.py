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
    print("SYMBOLS - which spelling /metal-quote accepts")
    ok = {}
    for metal, spellings in (("gold", ("XAU", "GOLD", "gold", "AU")),
                             ("nickel", ("XNI", "NI", "NICKEL", "Nickel", "LME-NI")),
                             ("aluminium", ("XAL", "AL", "ALU", "ALUMINUM", "Aluminum")),
                             ("tin", ("XSN", "SN", "TIN", "LME-TIN"))):
        for sym in spellings:
            code, body, _ = get("/metal-quote", symbol=sym, currency="USD")
            good = not (isinstance(body, dict) and body.get("error"))
            print(f"   {metal:9s} {sym:9s} HTTP {code} {str(body)[:260]}")
            if good:
                ok[metal] = sym
                break
    print(f"   accepted: {ok}\n")

    print("HISTORY - granularity of /metal-history over the last two days")
    end = int(time.time())
    for metal, sym in ok.items():
        for start_fmt in ("ms", "s", "iso"):
            lo = end - 2 * 86400
            st, en = {"ms": (lo * 1000, end * 1000), "s": (lo, end),
                      "iso": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(lo)),
                              time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)))}[start_fmt]
            code, body, _ = get("/metal-history", symbol=sym, currency="USD",
                                startTime=st, endTime=en)
            print(f"   {metal:9s} times as {start_fmt}: HTTP {code} {str(body)[:600]}")
            if not (isinstance(body, dict) and body.get("error")):
                break
    print()

    print("MOVEMENT - each accepted quote, five times a minute apart")
    for i in range(5):
        for metal, sym in ok.items():
            code, body, _ = get("/metal-quote", symbol=sym, currency="USD")
            print(f"   {time.strftime('%H:%M:%S', time.gmtime())} {metal:9s} {str(body)[:240]}")
        if i < 4:
            time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
