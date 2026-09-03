import price_monitor.test_notify as test_notify
from price_monitor.notifier import TelegramError


def test_main_fails_fast_without_credentials(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert test_notify.main() == 1
    assert "are not set" in capsys.readouterr().err


def test_main_sends_and_reports_success(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@some_channel")

    sent = {}

    def fake_send(token, chat_id, message):
        sent["token"] = token
        sent["chat_id"] = chat_id
        sent["message"] = message

    monkeypatch.setattr(test_notify, "send_telegram_message", fake_send)

    assert test_notify.main() == 0
    assert sent == {"token": "dummy-token", "chat_id": "@some_channel", "message": test_notify.MESSAGE}
    assert "@some_channel" in capsys.readouterr().out


def test_main_reports_telegram_error(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@some_channel")

    def fake_send(token, chat_id, message):
        raise TelegramError("boom")

    monkeypatch.setattr(test_notify, "send_telegram_message", fake_send)

    assert test_notify.main() == 1
    assert "boom" in capsys.readouterr().err
