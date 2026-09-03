from datetime import datetime, timedelta, timezone

from price_monitor import daily_signal_review
from price_monitor.config import AssetConfig, Config


def make_config():
    return Config(assets=[
        AssetConfig(symbol="EUR/USD", source="twelvedata", label="EUR/USD", news_query="euro dollar rate"),
        AssetConfig(symbol="BTC-USD", source="coinbase", label="Bitcoin", news_query="bitcoin price"),
    ])


def _calendar_event(title, when, country="USD", actual="", forecast="", previous=""):
    return {
        "title": title, "country": country, "date": when.isoformat(),
        "impact": "High", "actual": actual, "forecast": forecast, "previous": previous,
    }


def test_format_calendar_line_includes_only_present_details():
    with_details = _calendar_event(
        "Non-Farm Payrolls", datetime(2026, 1, 1, 13, 30, tzinfo=timezone.utc),
        actual="199K", forecast="426K", previous="249K")
    bare = _calendar_event("FOMC Member Speaks", datetime(2026, 1, 1, 13, 30, tzinfo=timezone.utc))

    with_details_line = daily_signal_review._format_calendar_line(with_details)
    bare_line = daily_signal_review._format_calendar_line(bare)

    assert "Non-Farm Payrolls" in with_details_line
    assert "actual: 199K" in with_details_line
    assert "forecast: 426K" in with_details_line
    assert "previous: 249K" in with_details_line
    assert "FOMC Member Speaks" in bare_line
    assert "(" not in bare_line


def test_format_event_lists_calendar_events_when_present():
    event = {"date": "2026-01-01", "return_pct": -2.5, "ewma_z": 4.1, "robust_z": 3.9, "notified": True}
    calendar_events = [_calendar_event("Non-Farm Payrolls", datetime(2026, 1, 1, 13, 30, tzinfo=timezone.utc))]

    text = daily_signal_review._format_event(event, [], calendar_events)

    assert "Non-Farm Payrolls" in text
    assert "no High-impact events found in this window" not in text


def test_format_event_reports_empty_calendar_window():
    event = {"date": "2026-01-01", "return_pct": -2.5, "ewma_z": 4.1, "robust_z": 3.9, "notified": False}
    text = daily_signal_review._format_event(event, [], [])
    assert "no High-impact events found in this window" in text
    assert "missed" in text


def test_build_review_only_includes_calendar_events_within_the_per_event_window(monkeypatch):
    """Window is [event-12h, event+1h] - see module docstring. Checks both
    boundaries land inclusive and anything further out is excluded."""
    cfg = make_config()
    monkeypatch.setattr(
        daily_signal_review, "fetch_event_headlines", lambda query, event_time, limit, delay=0.0, session=None: [])

    event_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    calendar_events = [
        _calendar_event("too early", event_time - timedelta(hours=12, minutes=1)),
        _calendar_event("lower bound", event_time - timedelta(hours=12)),
        _calendar_event("inside", event_time - timedelta(hours=1)),
        _calendar_event("upper bound", event_time + timedelta(hours=1)),
        _calendar_event("too late", event_time + timedelta(hours=1, minutes=1)),
    ]
    assets_report = [{
        "label": "EUR/USD", "symbol": "EUR/USD", "source": "twelvedata",
        "biggest_moves": [{
            "open_time": int(event_time.timestamp()), "date": "2026-01-01",
            "return_pct": 1.0, "ewma_z": 3.0, "robust_z": 3.0, "notified": True,
        }],
    }]

    review = daily_signal_review.build_review(cfg, assets_report, calendar_events, limit=5, delay=0.0, session=None)

    assert "lower bound" in review
    assert "inside" in review
    assert "upper bound" in review
    assert "too early" not in review
    assert "too late" not in review


def test_build_review_uses_each_assets_own_news_query(monkeypatch):
    cfg = make_config()
    queries_used = []

    def fake_fetch_headlines(query, event_time, limit, delay=0.0, session=None):
        queries_used.append(query)
        return []

    monkeypatch.setattr(daily_signal_review, "fetch_event_headlines", fake_fetch_headlines)

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

    review = daily_signal_review.build_review(cfg, assets_report, [], limit=5, delay=0.0, session=None)

    assert queries_used == ["euro dollar rate", "bitcoin price"]
    assert "## EUR/USD (EUR/USD) — recall 1/1" in review
    assert "## Bitcoin (BTC-USD) — recall 0/1" in review


def test_build_review_falls_back_to_label_when_asset_not_in_config(monkeypatch):
    cfg = make_config()
    monkeypatch.setattr(
        daily_signal_review, "fetch_event_headlines", lambda query, event_time, limit, delay=0.0, session=None: [])
    assets_report = [{
        "label": "Unknown Asset", "symbol": "XYZ", "source": "yahoo",
        "biggest_moves": [],
    }]
    review = daily_signal_review.build_review(cfg, assets_report, [], limit=5, delay=0.0, session=None)
    assert "Unknown Asset" in review
