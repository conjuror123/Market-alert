"""The morning dividend check: what lets an overnight gap be scored at all."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from price_monitor import ishares, yahoo
from jump import backfill, bars, corporate_actions
from jump.basket import Asset
from jump.sessions import Session

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 1)
TABLE = {DAY: Session(DAY, "09:30", "16:00", False)}


def fund(ticker):
    return Asset(ticker=ticker, source="twelvedata", block="credit",
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


def answer(monkeypatch, fake, splits=()):
    """Yahoo's answer: the payouts `fake` gives, and these split days."""
    monkeypatch.setattr(yahoo, "fetch_dividends_and_splits",
                        lambda *a, **k: (fake(*a, **k), list(splits)))


def run(files, now, answers, funds=("HYG", "GLD")):
    actions, checks = files
    return backfill.check_dividends([fund(t) for t in funds], TABLE, None, now=now,
                                    actions_path=actions, checks_path=checks)


def test_a_payout_found_this_morning_is_recorded_and_the_fund_vouched_for(files, monkeypatch):
    asked = []

    def fake(ticker, since, session=None, now=None):
        asked.append((ticker, since))
        return [(DAY, 0.0055)] if ticker == "HYG" else []

    answer(monkeypatch, fake)
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

    answer(monkeypatch, fake)
    r = run(files, at(10), None)

    assert r["failed"] == ["HYG"]
    checks = corporate_actions.load_checks(files[1])
    # Its gap stays unscored until a later run gets an answer.
    assert checks["HYG"] == "2026-09-30"
    assert checks["GLD"] == "2026-10-01"


def test_yahoo_not_answering_twice_in_a_row_ends_the_check(files, monkeypatch):
    # Each unanswered fund costs about 96 s of timeouts and retries; 133 funds
    # asked one by one would take the job past its 20 minutes.
    from price_monitor.models import Unreachable
    asked = []

    def fake(ticker, since, session=None, now=None):
        asked.append(ticker)
        raise Unreachable(f"{ticker}: Read timed out")

    answer(monkeypatch, fake)
    r = run(files, at(10), None, funds=("HYG", "GLD", "SLV", "TLT"))

    assert asked == ["HYG", "GLD"]
    assert r["failed"] == ["HYG", "GLD", "SLV", "TLT"]


def test_one_unanswered_fund_does_not_end_the_check(files, monkeypatch):
    from price_monitor.models import Unreachable
    asked = []

    def fake(ticker, since, session=None, now=None):
        asked.append(ticker)
        if ticker in ("HYG", "SLV"):
            raise Unreachable(f"{ticker}: Read timed out")
        return []

    answer(monkeypatch, fake)
    r = run(files, at(10), None, funds=("HYG", "GLD", "SLV", "TLT"))

    assert asked == ["HYG", "GLD", "SLV", "TLT"]
    assert r["failed"] == ["HYG", "SLV"]


def test_nothing_is_asked_before_the_open_or_off_a_trading_day(files, monkeypatch):
    answer(monkeypatch,
                        lambda *a, **k: pytest.fail("asked too early"))
    assert run(files, at(9, 5), None)["skipped"] == "before the open"
    saturday = datetime(2026, 10, 3, 11, tzinfo=NY).astimezone(timezone.utc)
    assert run(files, saturday, None)["skipped"] == "not a trading day"


def test_a_fund_already_confirmed_today_is_not_asked_again(files, monkeypatch):
    answer(monkeypatch, lambda *a, **k: [])
    run(files, at(10), None)
    answer(monkeypatch,
                        lambda *a, **k: pytest.fail("asked twice in one day"))
    assert run(files, at(11), None)["checked"] == 0


def test_a_fund_behind_for_days_is_named_once_a_day(files, monkeypatch):
    corporate_actions.write_checks({"HYG": "2026-09-20", "GLD": "2026-09-30"}, files[1])
    answer(monkeypatch,
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


def test_a_split_in_the_same_answer_is_declared(files, monkeypatch):
    # USHY's 6-for-5 of 2019-04-22 arrived unadjusted and read as -103 sigma:
    # 6:5 is not a ratio the guard knows. Declared, its gap is not scored.
    answer(monkeypatch, lambda t, *a, **k: [], splits=[DAY])
    run(files, at(10), None, funds=("HYG",))
    assert "2026-10-01" in corporate_actions.load_dividends(*files).splits["HYG"]


def test_yahoo_s_split_events_are_read_in_the_exchange_s_dates():
    # Yahoo's answer for USHY: the 6-for-5 stamped 13:30 UTC on 2019-04-22.
    payload = {"chart": {"result": [{"meta": {"exchangeTimezoneName": "America/New_York"},
                                     "events": {"splits": {"1555939800": {
                                         "date": 1555939800, "numerator": 6.0,
                                         "denominator": 5.0, "splitRatio": "6:5"}}}}]}}
    assert yahoo.parse_splits(payload, "USHY") == [date(2019, 4, 22)]
    assert yahoo.parse_splits({"chart": {"result": [{}]}}, "USHY") == []


# --- the funds abroad: each checked on its own market's clock ---------------------

def abroad(ticker, template):
    return Asset(ticker=ticker, source="yahoo", block="rates", has_volume=True,
                 tick_size=0.001, session_template=template, fetch_interval="30min",
                 label=ticker, in_basket=True)


ABROAD = [abroad("511090.SS", "shanghai"), abroad("IGLT.L", "london")]


def run_abroad(files, now, bars_dir="no-bars"):
    actions, checks = files
    return backfill.check_dividends_abroad(ABROAD, None, now=now, actions_path=actions,
                                           checks_path=checks, bars_dir=bars_dir)


def issuer(monkeypatch, asked, payouts=()):
    """iShares' answer for IGLT: these (ex-date, pounds) payouts."""
    monkeypatch.setattr(ishares, "fetch_payouts",
                        lambda ticker, session=None: asked.append(ticker) or list(payouts))


def test_a_fund_abroad_is_checked_after_its_own_markets_open_not_new_yorks(files, monkeypatch):
    # Shanghai opens at 09:30 (01:30 UTC), London at 08:00 (07:00 UTC in summer):
    # each fund is asked once its own session has opened, hours before New York's,
    # so the night it opened on is scored in the same run.
    asked = []

    def fake(ticker, since, session=None, now=None):
        asked.append(ticker)
        return [(date(2026, 10, 16), 0.0133)] if ticker == "511090.SS" else []

    answer(monkeypatch, fake)
    issuer(monkeypatch, asked)
    r = run_abroad(files, datetime(2026, 10, 16, 1, 5, tzinfo=timezone.utc))
    assert asked == [] and r["skipped"] == "nothing due"

    r = run_abroad(files, datetime(2026, 10, 16, 2, 5, tzinfo=timezone.utc))
    assert asked == ["511090.SS"] and r["checked"] == 1 and r["added"] == 1

    run_abroad(files, datetime(2026, 10, 16, 8, 5, tzinfo=timezone.utc))
    assert asked == ["511090.SS", "IGLT.L"]          # Shanghai's already checked today
    loaded = corporate_actions.load_dividends(*files)
    assert loaded.steps["511090.SS"]["2026-10-16"] == pytest.approx(0.0133)
    assert loaded.checked_through["511090.SS"] == "2026-10-16"
    assert loaded.checked_through["IGLT.L"] == "2026-10-16"


def test_a_fund_abroad_is_not_asked_on_a_day_its_market_is_shut(files, monkeypatch):
    # Golden Week: Shanghai shut on 2026-10-05, London open.
    asked = []
    answer(monkeypatch, lambda ticker, *a, **k: asked.append(ticker) or [])
    issuer(monkeypatch, asked)
    run_abroad(files, datetime(2026, 10, 5, 9, 5, tzinfo=timezone.utc))
    assert asked == ["IGLT.L"]
    assert "511090.SS" not in corporate_actions.load_checks(files[1])


def test_iglt_s_payouts_are_ishares_own_ex_dates_over_the_stored_close(files, monkeypatch, tmp_path):
    # Yahoo files some of IGLT's payouts under the record date, the day after the
    # price fell: IGLT is asked iShares, the China fund still Yahoo. iShares gives
    # pounds a share; the step is over the store's last close before the
    # ex-date's open (07:00 UTC in summer), and a payout not yet ex is left for
    # its own morning.
    hour = lambda *t: int(datetime(*t, tzinfo=timezone.utc).timestamp())
    bars.merge(bars.store_path(str(tmp_path), "yahoo_IGLT.L"), pd.DataFrame({
        "hour_utc": [hour(2026, 10, 15, 15), hour(2026, 10, 16, 7)],
        "open": [10.2, 9.99], "high": [10.2, 9.99], "low": [10.2, 9.99],
        "close": [10.21, 9.99], "volume": [1.0, 1.0], "n_src": [2, 2]}))
    by_yahoo, by_ishares = [], []
    answer(monkeypatch, lambda ticker, *a, **k: by_yahoo.append(ticker) or [])
    issuer(monkeypatch, by_ishares, [(date(2026, 5, 21), 0.2155), (date(2026, 10, 16), 0.21),
                                     (date(2026, 11, 12), 0.22)])
    r = run_abroad(files, datetime(2026, 10, 16, 8, 5, tzinfo=timezone.utc), str(tmp_path))
    assert by_yahoo == ["511090.SS"] and by_ishares == ["IGLT.L"]
    assert r["checked"] == 2 and r["added"] == 1
    loaded = corporate_actions.load_dividends(*files)
    assert loaded.steps["IGLT.L"] == {"2026-10-16": pytest.approx(0.21 / (10.21 - 0.21))}
    assert loaded.checked_through["IGLT.L"] == "2026-10-16"


def test_ishares_not_answering_leaves_iglt_unchecked_and_yahoo_still_asked(files, monkeypatch):
    by_yahoo = []
    answer(monkeypatch, lambda ticker, *a, **k: by_yahoo.append(ticker) or [])

    def down(ticker, session=None):
        raise backfill.Unreachable("IGLT.L: iShares answered 503")

    monkeypatch.setattr(ishares, "fetch_payouts", down)
    r = run_abroad(files, datetime(2026, 10, 16, 8, 5, tzinfo=timezone.utc))
    assert r["failed"] == ["IGLT.L"] and by_yahoo == ["511090.SS"]
    assert "IGLT.L" not in corporate_actions.load_checks(files[1])
