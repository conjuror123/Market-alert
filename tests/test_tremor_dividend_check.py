"""The morning dividend check: what lets an overnight gap be scored at all."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from price_monitor import yahoo
from tremor import backfill, corporate_actions
from tremor.basket import Asset
from tremor.sessions import Session

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 1)
TABLE = {DAY: Session(DAY, "09:30", "16:00", False)}


def fund(ticker):
    return Asset(ticker=ticker, source="twelvedata", tier=2, block="credit",
                 has_volume=True, tick_size=0.01, session_template="us_equity",
                 fetch_interval="30min", label=ticker, in_basket=True)


def at(hour, minute=5):
    return datetime(2026, 10, 1, hour, minute, tzinfo=NY).astimezone(timezone.utc)


@pytest.fixture
def files(tmp_path):
    actions, checks = tmp_path / "actions.csv", tmp_path / "checks.csv"
    corporate_actions.write_actions(str(actions), [corporate_actions.CorporateAction(
        "HYG", date(2026, 9, 1), "dividend", 0.00545)])
    corporate_actions.write_checks({"HYG": "2026-09-30", "GLD": "2026-09-30"}, str(checks))
    return str(actions), str(checks)


def run(files, now, answers, funds=("HYG", "GLD")):
    actions, checks = files
    return backfill.check_dividends([fund(t) for t in funds], TABLE, None, now=now,
                                    actions_path=actions, checks_path=checks)


def test_a_payout_found_this_morning_is_recorded_and_the_fund_vouched_for(files, monkeypatch):
    asked = []

    def fake(ticker, since, session=None, now=None):
        asked.append((ticker, since))
        return [(DAY, 0.0055)] if ticker == "HYG" else []

    monkeypatch.setattr(yahoo, "fetch_dividends", fake)
    r = run(files, at(10), None)

    assert r["checked"] == 2 and r["added"] == 1 and not r["failed"]
    # Asked from the day after the old checked-through date, not from scratch.
    assert asked == [("HYG", DAY), ("GLD", DAY)]
    loaded = corporate_actions.load_dividends(*files)
    assert loaded.steps["HYG"]["2026-10-01"] == pytest.approx(0.0055)
    assert loaded.checked_through == {"HYG": "2026-10-01", "GLD": "2026-10-01"}


def test_a_failed_fund_is_not_vouched_for(files, monkeypatch):
    def fake(ticker, since, session=None, now=None):
        if ticker == "HYG":
            raise yahoo.ExchangeError("HYG: status 503")
        return []

    monkeypatch.setattr(yahoo, "fetch_dividends", fake)
    r = run(files, at(10), None)

    assert r["failed"] == ["HYG"]
    checks = corporate_actions.load_checks(files[1])
    # Its gap stays unscored until a later run gets an answer.
    assert checks["HYG"] == "2026-09-30"
    assert checks["GLD"] == "2026-10-01"


def test_nothing_is_asked_before_the_open_or_off_a_trading_day(files, monkeypatch):
    monkeypatch.setattr(yahoo, "fetch_dividends",
                        lambda *a, **k: pytest.fail("asked too early"))
    assert run(files, at(9, 5), None)["skipped"] == "before the open"
    saturday = datetime(2026, 10, 3, 11, tzinfo=NY).astimezone(timezone.utc)
    assert run(files, saturday, None)["skipped"] == "not a trading day"


def test_a_fund_already_confirmed_today_is_not_asked_again(files, monkeypatch):
    monkeypatch.setattr(yahoo, "fetch_dividends", lambda *a, **k: [])
    run(files, at(10), None)
    monkeypatch.setattr(yahoo, "fetch_dividends",
                        lambda *a, **k: pytest.fail("asked twice in one day"))
    assert run(files, at(11), None)["checked"] == 0


def test_a_fund_behind_for_days_is_named_once_a_day(files, monkeypatch):
    corporate_actions.write_checks({"HYG": "2026-09-20", "GLD": "2026-09-30"}, files[1])
    monkeypatch.setattr(yahoo, "fetch_dividends",
                        lambda t, *a, **k: (_ for _ in ()).throw(
                            yahoo.ExchangeError("down")) if t == "HYG" else [])
    assert run(files, at(10), None)["stale"] == ["HYG"]
    # Not again at 11:05 - one line, not a stream.
    assert run(files, at(11), None)["stale"] == []


def test_yahoo_payouts_come_back_in_the_tables_form():
    # d = 0.435 / 79.8 on the previous close; the table stores d / (1 - d).
    ex = int(datetime(2026, 9, 1, 9, 30, tzinfo=NY).timestamp())
    before = int(datetime(2026, 8, 31, 9, 30, tzinfo=NY).timestamp())
    payload = {"chart": {"result": [{
        "meta": {"exchangeTimezoneName": "America/New_York"},
        "timestamp": [before, ex],
        "indicators": {"quote": [{"close": [79.8, 79.5]}]},
        "events": {"dividends": {str(ex): {"amount": 0.435, "date": ex}}},
    }]}}
    [(day, step)] = yahoo._parse_dividends(payload, "HYG")
    d = 0.435 / 79.8
    assert day == date(2026, 9, 1)
    assert step == pytest.approx(d / (1 - d))
