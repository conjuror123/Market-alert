import pytest

from price_monitor import news
from price_monitor.news import NewsError, fetch_news

SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<item>
<title>Bitcoin drops 5% amid market selloff</title>
<link>https://example.com/a</link>
<pubDate>Fri, 28 Aug 2026 12:00:00 GMT</pubDate>
<source>Example News</source>
</item>
<item>
<title>Analysts weigh in on crypto volatility</title>
<link>https://example.com/b</link>
<pubDate>Fri, 28 Aug 2026 10:00:00 GMT</pubDate>
<source>Another Source</source>
</item>
</channel>
</rss>
"""


class FakeResponse:
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="replace")


def test_parses_titles_links_and_dates(monkeypatch):
    def fake_get(url, params, timeout):
        return FakeResponse(200, SAMPLE_RSS)

    monkeypatch.setattr(news.requests, "get", fake_get)
    articles = fetch_news("Bitcoin")

    assert len(articles) == 2
    assert articles[0]["title"] == "Bitcoin drops 5% amid market selloff"
    assert articles[0]["link"] == "https://example.com/a"
    assert articles[0]["source"] == "Example News"
    assert articles[0]["published"].year == 2026


def test_limit_caps_results(monkeypatch):
    def fake_get(url, params, timeout):
        return FakeResponse(200, SAMPLE_RSS)

    monkeypatch.setattr(news.requests, "get", fake_get)
    assert len(fetch_news("Bitcoin", limit=1)) == 1


def test_non_200_status_raises(monkeypatch):
    def fake_get(url, params, timeout):
        return FakeResponse(503, b"")

    monkeypatch.setattr(news.requests, "get", fake_get)
    with pytest.raises(NewsError):
        fetch_news("Bitcoin")


def test_malformed_xml_raises(monkeypatch):
    def fake_get(url, params, timeout):
        return FakeResponse(200, b"not xml at all <<<")

    monkeypatch.setattr(news.requests, "get", fake_get)
    with pytest.raises(NewsError):
        fetch_news("Bitcoin")


def test_missing_pub_date_becomes_none(monkeypatch):
    raw = b"""<?xml version="1.0"?><rss><channel>
    <item><title>No date here</title><link>https://x</link></item>
    </channel></rss>"""

    def fake_get(url, params, timeout):
        return FakeResponse(200, raw)

    monkeypatch.setattr(news.requests, "get", fake_get)
    articles = fetch_news("Bitcoin")
    assert articles[0]["published"] is None
