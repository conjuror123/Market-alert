"""iShares' distribution table (price_monitor.ishares): the parser and the request."""
from datetime import date

import pytest

from price_monitor import ishares
from price_monitor.models import ExchangeError, Unreachable


def page(days, amounts):
    """The table as the page embeds it: HTML-escaped JSON, newest first, each
    array after its formatted copy."""
    def entry(name, values, shown):
        return ('"%s":{"name":"%s","visible":true,"sortOrder":3,"asOfDate":null,'
                '"formattedValue":[%s],"value":[%s],"returnType":false}'
                % (name, name, ",".join(f'"{v}"' for v in shown), ",".join(values)))
    data = ('{"dataPointsByNameMap":{'
            + entry("exDate", [str(d) for d in days], ["21/May/2026"] * len(days)) + ","
            + entry("totalDistribution", [f'"{a}"' for a in amounts], amounts) + "}}")
    return ('<html><div data-table="' + data.replace('"', "&quot;")
            + '"></div><script>"distroTable.totalDistribution.infobubble":""</script></html>')


def test_each_payout_is_its_ex_date_and_amount_oldest_first():
    text = page([20260521, 20251113, 20250515], ["0.2155", "0.2166", "0.2054"])
    assert ishares.parse(text, "IGLT.L") == [
        (date(2025, 5, 15), 0.2054), (date(2025, 11, 13), 0.2166), (date(2026, 5, 21), 0.2155)]


def test_a_page_without_the_table_or_with_uneven_arrays_raises():
    with pytest.raises(ExchangeError, match="no distribution table"):
        ishares.parse("<html>maintenance</html>", "IGLT.L")
    with pytest.raises(ExchangeError, match="2 ex-dates and 1 amounts"):
        ishares.parse(page([20260521, 20251113], ["0.2155"]), "IGLT.L")


class _Answer:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class _Session:
    def __init__(self, answers):
        self.answers, self.asked = list(answers), []

    def get(self, url, headers=None, timeout=None):
        self.asked.append((url, headers))
        return self.answers.pop(0)


def test_it_asks_the_fund_s_page_as_a_browser_and_retries_a_server_error(monkeypatch):
    monkeypatch.setattr(ishares.time, "sleep", lambda s: None)
    session = _Session([_Answer(503), _Answer(200, page([20260521], ["0.2155"]))])
    assert ishares.fetch_payouts("IGLT.L", session) == [(date(2026, 5, 21), 0.2155)]
    url, headers = session.asked[-1]
    assert "/251806/" in url and "siteEntryPassthrough=true" in url
    assert headers["User-Agent"].startswith("Mozilla/5.0")


def test_a_server_that_never_answers_is_unreachable_and_a_refusal_is_not(monkeypatch):
    monkeypatch.setattr(ishares.time, "sleep", lambda s: None)
    with pytest.raises(Unreachable):
        ishares.fetch_payouts("IGLT.L", _Session([_Answer(502)] * ishares.MAX_ATTEMPTS))
    with pytest.raises(ExchangeError) as refused:
        ishares.fetch_payouts("IGLT.L", _Session([_Answer(403)]))
    assert not isinstance(refused.value, Unreachable)
