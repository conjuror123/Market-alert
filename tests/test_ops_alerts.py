"""Operational Telegram: name who went dark, and keep it off the product channel."""
from jump.backfill import format_provider_failure, send_ops_alert


def test_a_dead_instrument_is_named_with_its_provider():
    text = format_provider_failure(
        [("twelvedata:SPY", "yahoo", "HTTP 500")])
    assert "twelvedata:SPY (yahoo): HTTP 500" in text
    assert "Загрузка баров: не обновились 1 инструмент" in text
    assert "Что делать:" in text


def test_ops_message_redacts_an_apikey_in_the_provider_error():
    text = format_provider_failure(
        [("twelvedata:SPY", "twelvedata",
          "https://api.twelvedata.com/time_series?apikey=sk-live")])
    assert "sk-live" not in text
    assert "apikey=&lt;redacted&gt;" in text
    text = format_provider_failure(
        [("twelvedata:SPY", "yahoo", "HTTP 500")])
    assert "twelvedata:SPY (yahoo): HTTP 500" in text
    assert "не обновились" in text


def test_a_spent_tiingo_budget_says_where_and_when_it_is_asked_again():
    text = format_provider_failure(
        [], tiingo_gone=True, tiingo_skipped=12,
        tiingo_remaining="0", tiingo_trip="twelvedata:XLK")
    head, why, means, todo = text.split("\n")
    assert head == "⚠️ <b>Tiingo: исчерпан лимит запросов (на twelvedata:XLK)</b>"
    assert why == ("Tiingo ответил отказом 429 («слишком много запросов»): по ответу Tiingo "
                   "осталось: 0; остальные инструменты Tiingo (12) в этот час пропущены.")
    assert means.startswith("Что это значит:") and todo.startswith("Что делать:")


def test_a_spent_budget_with_nothing_else_due_does_not_count_none_skipped():
    text = format_provider_failure(
        [], sifting_gone=True, sifting_skipped=0, sifting_trip="twelvedata:EUR/USD")
    head, why = text.split("\n")[:2]
    assert head == "⚠️ <b>SiftingIO: исчерпан лимит запросов (на twelvedata:EUR/USD)</b>"
    assert why == ("SiftingIO ответил отказом 429 («слишком много запросов»): сколько "
                   "осталось, SiftingIO в ответе не сказал.")


def test_a_missing_key_is_named_with_its_secret():
    text = format_provider_failure([], keys={"tiingo": ["TIINGO_API_KEY is not set", None, 27]})
    head, why, means, todo = text.split("\n")
    assert head == "🚨 <b>Tiingo: не задан ключ TIINGO_API_KEY</b>"
    assert why == ("В секретах GitHub Actions нет ключа TIINGO_API_KEY, поэтому Tiingo не "
                   "запрашивается совсем, и все его инструменты (27) остаются без новых "
                   "баров. Остальные источники работают как обычно.")
    assert "Settings → Secrets and variables → Actions" in todo


def test_a_refused_key_is_named_where_it_was_refused_and_first():
    text = format_provider_failure(
        [("twelvedata:SPY", "yahoo", "HTTP 500")],
        keys={"alpaca": ["KRE: Alpaca refused the key (403)", "twelvedata:KRE", 29]})
    assert text.startswith("⚠️ <b>Alpaca не принял ключ (на twelvedata:KRE)</b>\n"
                           "Ответ Alpaca: KRE: Alpaca refused the key (403); остальные "
                           "инструменты Alpaca (29) в этот час пропущены.\n")
    assert "ALPACA_KEY_ID / ALPACA_SECRET_KEY" in text


def test_a_yahoo_rate_limit_alone_says_nothing():
    # One refusal costs its instruments an hour, fetched again next run: said
    # only when one of them goes without a bar past its limit (stale).
    assert format_provider_failure([], yahoo_gone=True) == ""


def test_a_stale_instrument_of_a_refusing_yahoo_says_so():
    text = format_provider_failure([], yahoo_gone=True,
                                   stale=[("yahoo:CT=F", "yahoo", 37),
                                          ("twelvedata:UNG", "twelvedata", 15)])
    assert "Нет новых баров: 2 инструмента" in text
    assert "• yahoo:CT=F (yahoo, в этот час Yahoo отказал по лимиту): 37 ч торговли" in text
    assert "• twelvedata:UNG (twelvedata): 15 ч торговли" in text


def test_ops_alert_uses_the_health_chat_not_the_product_one(monkeypatch):
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@channel")
    monkeypatch.setenv("TELEGRAM_HEALTH_CHAT_ID", "12345")

    def fake_send(token, chat, text, **kwargs):
        sent.append((token, chat, text))
        return 1

    monkeypatch.setattr("price_monitor.notifier.send_telegram_message", fake_send)
    send_ops_alert("hello")
    assert sent == [("tok", "12345", "hello")]


def test_ops_alert_never_goes_to_the_channel(monkeypatch):
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@channel")
    monkeypatch.delenv("TELEGRAM_HEALTH_CHAT_ID", raising=False)
    monkeypatch.setattr(
        "price_monitor.notifier.send_telegram_message",
        lambda token, chat, text, **k: sent.append(chat) or 1)
    send_ops_alert("hello")
    assert sent == []


# Health goes out as HTML: an error quoted raw is a tag Telegram refuses, and
# the whole message with it. And past 4,096 characters Telegram refuses it too,
# so the wider the outage, the surer the silence.
CONNECT = ("SPY: HTTPSConnectionPool(host='query1.finance.yahoo.com', port=443): Max "
           "retries exceeded with url: /v8/finance/chart/SPY?interval=30m&period1="
           "1791137705&period2=1791224105 (Caused by ConnectTimeoutError(<urllib3."
           "connection.HTTPSConnection object at 0x7f3a2c1d5e50>, 'Connection to "
           "query1.finance.yahoo.com timed out. (connect timeout=30)'))")


def test_an_error_is_quoted_as_text_not_html():
    text = format_provider_failure(
        [("twelvedata:SPY", "tiingo", "unexpected status 403: <html><body>no</body>"),
         ("twelvedata:QQQ", "yahoo", CONNECT)],
        vote=["the vote failed (<Response [503]>)"])
    assert "<html>" not in text and "<urllib3" not in text and "<Response" not in text
    assert "&lt;html&gt;" in text and "&amp;period1=" in text


def test_a_wide_outage_fits_one_message(monkeypatch):
    from price_monitor import notifier
    sent = []
    monkeypatch.setattr(notifier, "send_telegram_message",
                        lambda token, chat, text, **k: sent.append(text) or 1)
    text = format_provider_failure(
        [(f"twelvedata:F{i}", "yahoo", CONNECT) for i in range(20)],
        silent={"yahoo": ["twelvedata:F1", 36], "sina": ["twelvedata:G1", 34]})
    assert notifier.send_health(text, "tok", "ops")
    assert len(sent[0]) <= notifier.TELEGRAM_LIMIT
    assert sent[0].splitlines()[-1].startswith("…и ещё ")
    # The summaries come first; a cut only ever trims the list of instruments.
    assert "yahoo не отвечает" in sent[0] and "sina не отвечает" in sent[0]


def test_a_health_message_that_cannot_be_sent_does_not_raise(monkeypatch):
    from price_monitor import notifier

    def refuse(token, chat, text, **kwargs):
        raise notifier.TelegramError("Telegram API error 400")

    monkeypatch.setattr(notifier, "send_telegram_message", refuse)
    assert notifier.send_health("hello", "tok", "ops") is False


def test_counts_take_their_russian_form():
    from price_monitor.notifier import instruments, plural
    assert [instruments(n) for n in (1, 2, 5, 11, 12, 21, 22, 25, 111)] == [
        "1 инструмент", "2 инструмента", "5 инструментов", "11 инструментов",
        "12 инструментов", "21 инструмент", "22 инструмента", "25 инструментов",
        "111 инструментов"]
    assert plural(0, "раз", "раза", "раз") == "0 раз"


def test_every_part_of_the_message_says_what_to_do():
    text = format_provider_failure(
        [("twelvedata:SPY", "yahoo", "HTTP 500")], tiingo_gone=True,
        silent={"sina": ["sina:CAD", 3]}, stale=[("yahoo:CT=F", "yahoo", 37)],
        keys={"tiingo": ["not set", None, 27]}, vote=["ℹ️ x"])
    parts = text.split("\n\n")
    assert len(parts) == 6
    assert all("Что делать" in p for p in parts if not p.startswith("🗳"))
