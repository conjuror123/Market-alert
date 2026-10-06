"""What one fetch asked of each provider: answers counted by host, and the quota
a provider says is left, so a budget running short shows in the log before a 429.

Counted on the HTTP session (a response hook), so no client changes. An answer
is counted whatever its status, since a 429 or a 500 uses quota too. A request
that times out has no answer and is not counted. Only what goes through a
session made here is seen.
"""
from __future__ import annotations

import threading
from collections import Counter
from urllib.parse import urlsplit

import requests

# Each provider by the host it is asked at. Anything else is named by its host.
PROVIDERS = {
    "api.tiingo.com": "tiingo",
    "api.sifting.io": "sifting",
    "api.twelvedata.com": "twelvedata",
    "data.alpaca.markets": "alpaca",
    "query1.finance.yahoo.com": "yahoo",
    "data-api.binance.vision": "binance",
    "www.google.com": "google",
    "finance.sina.com.cn": "sina",
    "stock.finance.sina.com.cn": "sina",
    "gu.sina.cn": "sina",
    "api.wsj.net": "marketwatch",
    "api.stlouisfed.org": "fred",
    "cdn.cboe.com": "cboe",
    "api.hfdatalibrary.com": "hfdata",
    "datafeed.dukascopy.com": "dukascopy",
}

# The headers a provider says its remaining quota in: Tiingo, SiftingIO, Twelve Data.
REMAINING = ("X-RateLimit-Remaining", "X-Quota-Remaining", "api-credits-left")


class Usage:
    """One run's count. Thread-safe: the Twelve Data batch runs in a thread."""

    def __init__(self) -> None:
        self.answers: Counter = Counter()
        self.left: dict[str, str] = {}
        self._lock = threading.Lock()

    def session(self) -> requests.Session:
        """A session whose every answer is counted here."""
        session = requests.Session()
        session.hooks["response"].append(self.seen)
        return session

    def seen(self, response, *args, **kwargs):
        host = urlsplit(response.url).hostname or "?"
        provider = PROVIDERS.get(host, host)
        left = next((response.headers[h] for h in REMAINING if h in response.headers), None)
        with self._lock:
            self.answers[provider] += 1
            if left is not None:
                self.left[provider] = left
        return response

    def line(self) -> str:
        """`requests: tiingo 27 (left 4973), sifting 17, ...`, most asked first."""
        if not self.answers:
            return "requests: none"
        parts = []
        for provider, n in sorted(self.answers.items(), key=lambda kv: (-kv[1], kv[0])):
            left = self.left.get(provider)
            parts.append(f"{provider} {n}" + (f" (left {left})" if left is not None else ""))
        return "requests: " + ", ".join(parts)
