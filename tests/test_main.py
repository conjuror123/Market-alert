"""What is left of the hourly pass: deliver, and report when that breaks."""
from types import SimpleNamespace

import pytest

from price_monitor import __main__ as entry


def _cfg(tmp_path):
    return SimpleNamespace(
        state_path=str(tmp_path / "state.json"),
        telegram_bot_token="t", telegram_chat_id="c",
        health_alert_after_failures=2, health_reminder_every_failures=5,
    )


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """Nothing in these tests may reach Telegram or the network."""
    sent = []
    monkeypatch.setattr(entry, "send_telegram_message",
                        lambda token, chat, text: sent.append(text) or 1)
    monkeypatch.setattr(entry.weekly_digest, "maybe_send_weekly_digest",
                        lambda *a, **k: 0)
    monkeypatch.setattr(entry.weekly_digest, "maybe_refresh_calendar",
                        lambda *a, **k: False)
    monkeypatch.setattr(entry.requests, "Session", lambda: object())
    return sent


def test_a_clean_run_delivers_and_reports_nothing(tmp_path, monkeypatch, _quiet):
    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    assert entry.main() == 0
    assert _quiet == []


def test_a_delivery_failure_does_not_bring_the_run_down(tmp_path, monkeypatch, _quiet):
    # The run still finishes and still saves its state; the failure is carried
    # into the health report rather than raised, because a raise here would lose
    # the very record that says anything is wrong.
    def boom(cfg, state):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", boom)
    assert entry.main() == 1
    assert entry.load_state(str(tmp_path / "state.json")) != {}


def test_the_health_alert_waits_for_the_configured_streak(tmp_path, monkeypatch, _quiet):
    def boom(cfg, state):
        raise RuntimeError("no")

    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", boom)
    entry.main()
    assert _quiet == []                      # one failure is not yet news
    entry.main()
    assert len(_quiet) == 1 and "failing for 2" in _quiet[0]


def test_recovery_is_announced_only_after_a_reported_outage(tmp_path, monkeypatch, _quiet):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(entry, "load_config", lambda: cfg)

    def boom(cfg_, state):
        raise RuntimeError("no")

    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", boom)
    entry.main()
    entry.main()
    _quiet.clear()
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg_, state: 0)
    assert entry.main() == 0
    assert len(_quiet) == 1 and "recovered" in _quiet[0]


def test_a_broken_weekly_digest_is_also_carried_into_health(tmp_path, monkeypatch, _quiet):
    def boom(*a, **k):
        raise RuntimeError("calendar is down")

    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.weekly_digest, "maybe_send_weekly_digest", boom)
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    assert entry.main() == 1
