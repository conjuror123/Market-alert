"""Operational Telegram: name who went dark, and keep it off the product channel."""
from tremor.backfill import format_provider_failure, send_ops_alert


def test_a_dead_instrument_is_named_with_its_provider():
    text = format_provider_failure(
        [("twelvedata:SPY", "yahoo", "HTTP 500")])
    assert "twelvedata:SPY (yahoo): HTTP 500" in text
    assert "went dark" in text


def test_ops_message_redacts_an_apikey_in_the_provider_error():
    text = format_provider_failure(
        [("twelvedata:SPY", "twelvedata",
          "https://api.twelvedata.com/time_series?apikey=sk-live")])
    assert "sk-live" not in text
    assert "apikey=&lt;redacted&gt;" in text
    text = format_provider_failure(
        [("twelvedata:SPY", "yahoo", "HTTP 500")])
    assert "twelvedata:SPY (yahoo): HTTP 500" in text
    assert "went dark" in text


def test_tiingo_bucket_pressure_is_in_the_same_message():
    text = format_provider_failure(
        [], tiingo_gone=True, tiingo_skipped=12,
        tiingo_remaining="0", tiingo_trip="twelvedata:XLK")
    assert "remaining headroom 0" in text
    assert "12 remaining Tiingo" in text
    assert "twelvedata:XLK" in text
    assert "not switched automatically" in text


def test_yahoo_rate_limit_is_in_the_same_message():
    text = format_provider_failure(
        [], yahoo_gone=True, yahoo_skipped=14, yahoo_trip="twelvedata:UGA")
    assert "14 remaining Yahoo" in text
    assert "twelvedata:UGA" in text
    assert "not switched automatically" in text


def test_ops_alert_uses_the_health_chat_not_the_product_one(monkeypatch):
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@channel")
    monkeypatch.setenv("TELEGRAM_HEALTH_CHAT_ID", "12345")

    def fake_send(token, chat, text):
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
        lambda token, chat, text: sent.append(chat) or 1)
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
        second_source=["the second-source check failed (<Response [503]>)"])
    assert "<html>" not in text and "<urllib3" not in text and "<Response" not in text
    assert "&lt;html&gt;" in text and "&amp;period1=" in text


def test_a_wide_outage_fits_one_message(monkeypatch):
    from price_monitor import notifier
    sent = []
    monkeypatch.setattr(notifier, "send_telegram_message",
                        lambda token, chat, text: sent.append(text) or 1)
    text = format_provider_failure(
        [(f"twelvedata:F{i}", "yahoo", CONNECT) for i in range(20)],
        silent={"yahoo": ["twelvedata:F1", 36], "sina": ["twelvedata:G1", 34]})
    assert notifier.send_health(text, "tok", "ops")
    assert len(sent[0]) <= notifier.TELEGRAM_LIMIT
    assert sent[0].splitlines()[-1].startswith("…and ")
    # The summaries come first; a cut only ever trims the list of instruments.
    assert "yahoo did not answer" in sent[0] and "sina did not answer" in sent[0]


def test_a_health_message_that_cannot_be_sent_does_not_raise(monkeypatch):
    from price_monitor import notifier

    def refuse(token, chat, text):
        raise notifier.TelegramError("Telegram API error 400")

    monkeypatch.setattr(notifier, "send_telegram_message", refuse)
    assert notifier.send_health("hello", "tok", "ops") is False
