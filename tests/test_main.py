"""What is left of the hourly pass: deliver, and report when that breaks."""
from types import SimpleNamespace

import pytest

from price_monitor import __main__ as entry


def _cfg(tmp_path, health_chat="ops"):
    return SimpleNamespace(
        state_path=str(tmp_path / "state.json"),
        telegram_bot_token="t", telegram_chat_id="c",
        telegram_health_chat_id=health_chat,
        health_alert_after_failures=2, health_reminder_every_failures=5,
    )


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """Nothing in these tests may reach Telegram or the network."""
    sent = []
    from price_monitor import notifier
    monkeypatch.setattr(notifier, "send_telegram_message",
                        lambda token, chat, text: sent.append((chat, text)) or 1)
    monkeypatch.setattr(entry.weekly_digest, "maybe_send_weekly_digest",
                        lambda *a, **k: 0)
    monkeypatch.setattr(entry.weekly_digest, "maybe_refresh_calendar",
                        lambda *a, **k: False)
    monkeypatch.setattr(entry.requests, "Session", lambda: object())
    return sent


def test_health_down_redacts_secrets_in_error_details():
    text = entry.format_health_down(3, [
        "https://api.telegram.org/bot999:AAA/sendMessage failed",
        "provider said apikey=sk-live",
    ])
    assert "bot999:AAA" not in text
    assert "sk-live" not in text
    assert "bot&lt;redacted&gt;" in text
    assert "apikey=&lt;redacted&gt;" in text


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
    assert len(_quiet) == 1 and "failing for 2" in _quiet[0][1]
    assert _quiet[0][0] == "ops"


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
    assert len(_quiet) == 1 and "recovered" in _quiet[0][1]
    assert _quiet[0][0] == "ops"


def test_the_streak_is_about_the_run_not_one_instrument(tmp_path, monkeypatch, _quiet):
    # A run that delivered but lost a fund in the backfill used to leave the
    # streak where it was: it never said "recovered", and a second outage
    # started from the first's count and went unreported until 27. The fund
    # has its own line every run; the streak counts what fails the run.
    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    # What the workflow told the monitor when a fund went dark - every run here.
    # It no longer reads it; the test fails if it ever does again.
    monkeypatch.setenv("TREMOR_STEP_FAILED", "true")

    def runs(n, crashed):
        monkeypatch.setenv("TREMOR_PIPELINE_CRASHED", "true" if crashed else "false")
        for _ in range(n):
            entry.main()

    runs(2, crashed=True)
    assert len(_quiet) == 1 and "failing for 2" in _quiet[0][1]
    runs(3, crashed=False)
    assert len(_quiet) == 2 and "recovered" in _quiet[1][1]
    runs(2, crashed=True)
    assert len(_quiet) == 3 and "failing for 2" in _quiet[2][1]


def test_a_corrupt_state_file_stops_the_run(tmp_path, monkeypatch, _quiet):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    called = []
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver",
                        lambda *a, **k: called.append(True) or 0)
    assert entry.main() == 2
    assert called == []


def test_a_broken_weekly_digest_is_also_carried_into_health(tmp_path, monkeypatch, _quiet):
    def boom(*a, **k):
        raise RuntimeError("calendar is down")

    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.weekly_digest, "maybe_send_weekly_digest", boom)
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    assert entry.main() == 1


def test_a_pipeline_that_crashed_is_a_failure_and_alerts_after_the_streak(
        tmp_path, monkeypatch, _quiet):
    # A backfill that lost an instrument is not a failure of the run - its own
    # alert names who went dark. A pipeline or detector that crashed refreshed
    # no events at all, and that must reach the health chat like any failure.
    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    monkeypatch.setenv("TREMOR_PIPELINE_CRASHED", "true")
    assert entry.main() == 1
    assert _quiet == []                          # the first failure waits for the streak
    assert entry.main() == 1
    assert len(_quiet) == 1 and _quiet[0][0] == "ops"
    assert "events were not refreshed" in _quiet[0][1]


def test_a_step_stopped_part_way_says_where_and_after_how_long(
        tmp_path, monkeypatch, _quiet):
    # A crash and a step out of time look the same to the workflow; the stage
    # file says which part was running and for how long.
    import time
    stages = tmp_path / "stages"
    start = int(time.time()) - 12 * 60
    stages.write_text(f"fetch {start}\npipeline {start + 600}\n")
    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    monkeypatch.setenv("TREMOR_PIPELINE_CRASHED", "true")
    monkeypatch.setenv("TREMOR_STAGE_FILE", str(stages))
    entry.main()
    entry.main()
    assert "stopped during pipeline, 12 min after it began" in _quiet[0][1]


@pytest.mark.parametrize("env, words", [
    ("SESSIONS_STEP_OUTCOME", "session table could not be extended"),
    ("HOT_SAVE_STEP_OUTCOME", "open months of the bars were not saved"),
])
def test_a_side_step_that_failed_counts_as_a_failed_run(tmp_path, monkeypatch, _quiet,
                                                       env, words):
    # Neither stops the run, so nothing else would ever say so.
    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    monkeypatch.setenv(env, "failure")
    assert entry.main() == 1
    assert entry.main() == 1
    assert words in _quiet[0][1]


def test_side_steps_that_succeeded_or_did_not_run_are_clean(tmp_path, monkeypatch, _quiet):
    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    monkeypatch.setenv("SESSIONS_STEP_OUTCOME", "success")
    monkeypatch.setenv("HOT_SAVE_STEP_OUTCOME", "skipped")
    assert entry.main() == 0


def test_hours_without_a_run_are_named_by_the_next_run(tmp_path, monkeypatch, _quiet):
    # 761 runs from 2026-08-28 to 10-05 began 60 min apart, but for three
    # stretches none began at all - one of them six days long - and no message
    # said so: the runs that would have were the ones missing.
    import time
    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    state = {"_monitoring_health": {"consecutive_failures": 0,
                                    "last_run_utc": int(time.time()) - 5 * 3600}}
    entry.save_state(str(tmp_path / "state.json"), state)
    assert entry.main() == 0
    assert len(_quiet) == 1 and "4 hourly run(s) missing" in _quiet[0][1]
    # The next run, an hour on, has nothing to say.
    assert entry.main() == 0
    assert len(_quiet) == 1


def test_an_hour_and_a_bit_between_runs_is_not_a_missed_run(tmp_path, monkeypatch, _quiet):
    import time
    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    state = {"_monitoring_health": {"consecutive_failures": 0,
                                    "last_run_utc": int(time.time()) - 72 * 60}}
    entry.save_state(str(tmp_path / "state.json"), state)
    assert entry.main() == 0
    assert _quiet == []


def test_the_first_run_ever_names_no_gap(tmp_path, monkeypatch, _quiet):
    monkeypatch.setattr(entry, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(entry.tremor_delivery, "maybe_deliver", lambda cfg, state: 0)
    assert entry.main() == 0
    assert _quiet == []
    assert entry.load_state(str(tmp_path / "state.json"))["_monitoring_health"]["last_run_utc"]


def test_the_down_alert_quotes_its_errors_and_fits():
    from price_monitor import notifier
    text = entry.format_health_down(3, ["Tremor delivery failed (<html>" + "x" * 900 + ")"] * 10)
    assert "<html>" not in text
    assert len(text) <= notifier.TELEGRAM_LIMIT
