from datetime import datetime, timedelta, timezone

from price_monitor import calibration_review
from price_monitor.config import AssetConfig, Config
from price_monitor.news import NewsError


def make_config():
    return Config(assets=[
        AssetConfig(symbol="EUR/USD", source="twelvedata", label="EUR/USD", news_query="euro dollar rate"),
        AssetConfig(symbol="BTC-USD", source="coinbase", label="Bitcoin", news_query="bitcoin price"),
    ])


def test_fetch_event_headlines_scopes_to_the_6_to_12h_window(monkeypatch):
    event_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    calls = []

    def fake_fetch_news(query, limit=6, session=None):
        calls.append((query, limit))
        return [
            {"title": "too early", "source": "", "link": "", "published": event_time + timedelta(hours=3)},
            {"title": "in window", "source": "Reuters", "link": "", "published": event_time + timedelta(hours=8)},
        ]

    monkeypatch.setattr(calibration_review, "fetch_news", fake_fetch_news)
    headlines = calibration_review.fetch_event_headlines("euro dollar rate", event_time, limit=5)

    assert [h["title"] for h in headlines] == ["in window"]
    # Pulls a generous candidate pool *before* the exact-window filter, not
    # just the final desired headline count - see _CANDIDATE_POOL_SIZE.
    assert calls[0][1] == calibration_review._CANDIDATE_POOL_SIZE


def test_fetch_event_headlines_trims_to_limit_after_filtering(monkeypatch):
    """The real bug this guards against: passing the final headline count as
    the fetch_news limit meant a whole day's worth of Google results got cut
    to a handful *before* the precise [event+6h, event+12h] filter ran, so a
    genuinely newsy event could show only 1-2 headlines instead of up to
    `limit` - starved by under-fetching, not by the window being too narrow."""
    event_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    in_window = [
        {"title": f"story {i}", "source": "", "link": "", "published": event_time + timedelta(hours=7)}
        for i in range(8)
    ]

    monkeypatch.setattr(calibration_review, "fetch_news", lambda query, limit=6, session=None: in_window)
    headlines = calibration_review.fetch_event_headlines("euro dollar rate", event_time, limit=5)

    assert len(headlines) == 5


def test_fetch_event_headlines_returns_empty_on_news_error(monkeypatch):
    def failing_fetch(query, limit=6, session=None):
        raise NewsError("boom")

    monkeypatch.setattr(calibration_review, "fetch_news", failing_fetch)
    headlines = calibration_review.fetch_event_headlines(
        "euro dollar rate", datetime(2026, 1, 1, tzinfo=timezone.utc), limit=5)
    assert headlines == []


def test_format_event_marks_caught_and_missed():
    caught = {"date": "2026-01-01", "return_pct": -2.5, "ewma_z": 4.1, "robust_z": 3.9, "notified": True}
    missed = {"date": "2026-01-02", "return_pct": 1.8, "ewma_z": 2.2, "robust_z": 2.5, "notified": False}

    caught_text = calibration_review._format_event(caught, [])
    missed_text = calibration_review._format_event(missed, [{"title": "x", "source": "", "published": None}])

    assert "ПОЙМАНО" in caught_text
    assert "новостей не найдено" in caught_text
    assert "пропущено" in missed_text
    assert "x" in missed_text


def test_build_review_uses_each_assets_own_news_query(monkeypatch):
    cfg = make_config()
    queries_used = []

    def fake_fetch_headlines(query, event_time, limit, session=None):
        queries_used.append(query)
        return []

    monkeypatch.setattr(calibration_review, "fetch_event_headlines", fake_fetch_headlines)

    assets_report = [
        {
            "label": "EUR/USD", "symbol": "EUR/USD", "source": "twelvedata",
            "biggest_moves": [
                {"open_time": 0, "date": "2021-01-01", "return_pct": 1.0, "ewma_z": 3.0, "robust_z": 3.0, "notified": True},
            ],
        },
        {
            "label": "Bitcoin", "symbol": "BTC-USD", "source": "coinbase",
            "biggest_moves": [
                {"open_time": 0, "date": "2021-01-01", "return_pct": -5.0, "ewma_z": 6.0, "robust_z": 6.0, "notified": False},
            ],
        },
    ]

    review = calibration_review.build_review(cfg, assets_report, limit=5, delay=0.0, session=None)

    assert queries_used == ["euro dollar rate", "bitcoin price"]
    assert "## EUR/USD (EUR/USD) — recall 1/1" in review
    assert "## Bitcoin (BTC-USD) — recall 0/1" in review


def test_build_review_falls_back_to_label_when_asset_not_in_config():
    cfg = make_config()
    assets_report = [{
        "label": "Unknown Asset", "symbol": "XYZ", "source": "yahoo",
        "biggest_moves": [],
    }]
    review = calibration_review.build_review(cfg, assets_report, limit=5, delay=0.0, session=None)
    assert "Unknown Asset" in review
