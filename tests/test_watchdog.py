"""price_monitor.watchdog: the weekly check that the hourly run still runs."""
import json
from datetime import datetime, timezone

from price_monitor import health, notifier, watchdog

NOW = int(datetime(2026, 11, 9, 10, 41, tzinfo=timezone.utc).timestamp())


def _state(tmp_path, last=None, text=None):
    path = tmp_path / "state.json"
    if text is not None:
        path.write_text(text)
    elif last is not None:
        path.write_text(json.dumps({health.STATE_KEY: {"last_run_utc": last}}))
    return str(path)


def _sent(monkeypatch, ok=True):
    sent = []
    monkeypatch.setattr(notifier, "send_health", lambda text, *a, **k: sent.append(text) or ok)
    return sent


def test_a_current_run_says_nothing(tmp_path, monkeypatch):
    sent = _sent(monkeypatch)
    assert watchdog.main(_state(tmp_path, NOW - 36 * 60), NOW) == 0
    assert sent == []


def test_a_run_stopped_for_days_is_told_to_the_health_chat(tmp_path, monkeypatch):
    sent = _sent(monkeypatch)
    last = int(datetime(2026, 11, 6, 7, 5, tzinfo=timezone.utc).timestamp())
    assert watchdog.main(_state(tmp_path, last), NOW) == 0
    assert len(sent) == 1
    assert "hourly run has stopped" in sent[0] and "2026-11-06 07:05 UTC, 3 days ago" in sent[0]
    assert "cron-job.org" in sent[0]


def test_no_record_and_an_unreadable_file_are_told_too(tmp_path, monkeypatch):
    sent = _sent(monkeypatch)
    assert watchdog.main(str(tmp_path / "missing.json"), NOW) == 0
    assert watchdog.main(_state(tmp_path, text="{trunc"), NOW) == 0
    assert len(sent) == 2 and all("no run on record" in s for s in sent)


def test_a_health_chat_that_cannot_be_told_fails_the_job(tmp_path, monkeypatch):
    _sent(monkeypatch, ok=False)
    assert watchdog.main(_state(tmp_path, NOW - 5 * 3600), NOW) == 1
