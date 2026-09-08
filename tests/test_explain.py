import os
from datetime import datetime, timedelta, timezone

from price_monitor import explain
from price_monitor.alerts_log import load_alerts_log, record_sent_alert, save_alerts_log
from price_monitor.config import Config
from price_monitor.explain import (
    _filter_after,
    _filter_before,
    _is_old_enough,
    _scope_query,
    _select_todo,
)
from price_monitor.llm import LLMError
from price_monitor.news import NewsError
from price_monitor.notifier import TelegramError


def make_config(tmp_path, llm_api_key="secret-key"):
    return Config(
        telegram_bot_token="bot-token",
        telegram_chat_id="@chan",
        alerts_log_path=os.path.join(tmp_path, "alerts_log.json"),
        llm_api_key=llm_api_key,
    )


def seed_pending_entry(path, sent_at=None, message_id=42):
    """Defaults to an alert well past the default 12h min-age gate, since most
    tests here care about what happens once an entry is actually eligible for
    processing - tests for the age gate itself pass an explicit sent_at."""
    entries = []
    record_sent_alert(
        entries,
        chat_id="@chan",
        message_id=message_id,
        symbol="Ethereum",
        message_text="Ethereum - unusual market move",
        last_close=2473.66,
        last_return_pct=-1.45,
        ewma_z=-3.60,
        robust_z=-3.61,
        volume_z=0.70,
        now=sent_at or (datetime.now(timezone.utc) - timedelta(hours=14)),
    )
    save_alerts_log(path, entries)


def test_missing_api_key_returns_error_without_any_calls(tmp_path, monkeypatch, capsys):
    cfg = make_config(tmp_path, llm_api_key="")
    monkeypatch.setattr(explain, "load_config", lambda: cfg)

    def boom(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(explain, "fetch_news", boom)
    monkeypatch.setattr(explain, "chat_completion", boom)
    monkeypatch.setattr(explain, "edit_telegram_message", boom)

    assert explain.main() == 1
    assert "LLM_API_KEY" in capsys.readouterr().err


def test_main_requires_message_id_env(tmp_path, monkeypatch, capsys):
    """There is no "process everything" mode - a run always needs an explicit
    Message ID, so a single click can never spend tokens on an entire backlog
    of pending alerts at once."""
    cfg = make_config(tmp_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.delenv("EXPLAIN_MESSAGE_ID", raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(explain, "fetch_news", boom)
    monkeypatch.setattr(explain, "chat_completion", boom)
    monkeypatch.setattr(explain, "edit_telegram_message", boom)

    assert explain.main() == 1
    assert "Message ID" in capsys.readouterr().err


def test_full_flow_explains_and_edits_message(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "42")
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [
        {"title": "Ethereum falls on macro selloff", "source": "Example",
         "published": datetime.now(timezone.utc), "link": ""}
    ])
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "The fall is tied to a broad sell-off across the market.")

    edits = []
    monkeypatch.setattr(
        explain, "edit_telegram_message",
        lambda token, chat_id, message_id, text: edits.append((token, chat_id, message_id, text)),
    )

    assert explain.main() == 0
    assert len(edits) == 1
    token, chat_id, message_id, text = edits[0]
    assert (chat_id, message_id) == ("@chan", 42)
    assert "Ethereum - unusual market move" in text
    assert "The fall is tied to a broad sell-off across the market." in text

    saved = load_alerts_log(cfg.alerts_log_path)
    assert saved[0]["explained"] is True
    assert saved[0]["explanation"] == "The fall is tied to a broad sell-off across the market."


def test_full_flow_saves_model_and_request_messages_for_later_debugging(tmp_path, monkeypatch):
    """If a bogus explanation ever shows up, this is what lets you check
    afterwards what was actually sent to the LLM and which model answered -
    without it, that information is gone once the run's Actions log expires."""
    cfg = make_config(tmp_path)
    cfg.llm_model_peak = "flash-x"
    cfg.llm_model_offpeak = "pro-x"
    alert_time = datetime.now(timezone.utc) - timedelta(hours=14)
    seed_pending_entry(cfg.alerts_log_path, sent_at=alert_time)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "42")
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [
        {"title": "Ethereum falls on macro selloff", "source": "Example",
         "published": alert_time + timedelta(hours=8), "link": ""}
    ])
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "The fall is tied to a broad sell-off.")
    monkeypatch.setattr(explain, "edit_telegram_message", lambda *a, **k: None)

    assert explain.main() == 0
    saved = load_alerts_log(cfg.alerts_log_path)[0]
    assert saved["llm_model"] in ("flash-x", "pro-x")
    assert saved["llm_messages"][0]["role"] == "system"
    assert saved["llm_messages"][1]["role"] == "user"
    assert "Ethereum falls on macro selloff" in saved["llm_messages"][1]["content"]


def test_news_fetch_failure_still_asks_llm(tmp_path, monkeypatch):
    """The window is fixed relative to the alert, so unlike the old "up to
    now" search, there's no value in skipping and retrying later - the LLM is
    asked anyway (with no headlines), same as when the search simply finds
    nothing."""
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "42")

    def failing_fetch(query, limit=6):
        raise NewsError("boom")

    monkeypatch.setattr(explain, "fetch_news", failing_fetch)
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "No clear cause was found in the news.")
    monkeypatch.setattr(explain, "edit_telegram_message", lambda *a, **k: None)

    assert explain.main() == 0
    saved = load_alerts_log(cfg.alerts_log_path)
    assert saved[0]["explained"] is True


def test_llm_error_leaves_entry_pending_for_retry(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "42")
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [
        {"title": "some headline", "source": "", "link": "", "published": datetime.now(timezone.utc)}
    ])

    def failing_llm(**kwargs):
        raise LLMError("rate limited")

    monkeypatch.setattr(explain, "chat_completion", failing_llm)
    monkeypatch.setattr(explain, "edit_telegram_message", lambda *a, **k: None)

    assert explain.main() == 1
    saved = load_alerts_log(cfg.alerts_log_path)
    assert saved[0]["explained"] is False


def test_telegram_edit_error_leaves_entry_pending_for_retry(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "42")
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [
        {"title": "some headline", "source": "", "link": "", "published": datetime.now(timezone.utc)}
    ])
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "explanation")

    def failing_edit(*args, **kwargs):
        raise TelegramError("edit failed")

    monkeypatch.setattr(explain, "edit_telegram_message", failing_edit)

    assert explain.main() == 1
    saved = load_alerts_log(cfg.alerts_log_path)
    assert saved[0]["explained"] is False


def test_explain_entry_passes_time_based_model_choice(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    cfg.llm_model_peak = "flash-x"
    cfg.llm_model_offpeak = "pro-x"
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [
        {"title": "some headline", "source": "", "link": "",
         "published": datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)}
    ])
    monkeypatch.setattr(explain, "select_model", lambda peak, offpeak, now=None: f"{peak}/{offpeak}")

    captured = {}

    def fake_chat_completion(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(explain, "chat_completion", fake_chat_completion)

    entry = {
        "symbol": "Ethereum", "last_return_pct": -1.0, "last_close": 100.0,
        "sent_at": "2026-01-01T00:00:00+00:00",
    }
    explain.explain_entry(cfg, entry, "Ethereum")

    assert captured["model"] == "flash-x/pro-x"


def test_html_special_characters_in_explanation_are_escaped(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "42")
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [
        {"title": "some headline", "source": "", "link": "", "published": datetime.now(timezone.utc)}
    ])
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "Price fell <5% on volume > normal & with no clear cause")

    edits = []
    monkeypatch.setattr(
        explain, "edit_telegram_message",
        lambda token, chat_id, message_id, text: edits.append(text),
    )

    explain.main()
    assert "<5%" not in edits[0]
    assert "&lt;5%" in edits[0]
    assert "&gt;" in edits[0]
    assert "&amp;" in edits[0]


def test_scope_query_adds_after_and_before_in_pacific_calendar_days():
    lower_cutoff = datetime(2026, 8, 28, 14, 16, tzinfo=timezone.utc)
    upper_cutoff = datetime(2026, 8, 30, 14, 16, tzinfo=timezone.utc)
    # 14:16 UTC in August is 07:16 America/Los_Angeles (PDT, UTC-7) - same
    # Pacific calendar day as the UTC one here, so after: matches exactly;
    # before: is the day *after* upper_cutoff's Pacific day (see docstring).
    assert (
        _scope_query("Ethereum", lower_cutoff, upper_cutoff)
        == "Ethereum after:2026-08-28 before:2026-08-31"
    )


def test_scope_query_after_moves_to_previous_pacific_day_late_in_utc_day():
    # 02:00 UTC is 19:00 the *previous* day in America/Los_Angeles (PDT) -
    # after: must use that earlier Pacific date, or it would exclude articles
    # published between 19:00 Pacific and the UTC-day rollover.
    lower_cutoff = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
    assert _scope_query("Ethereum", lower_cutoff, None) == "Ethereum after:2026-08-27"


def test_scope_query_unchanged_when_lower_cutoff_unknown():
    assert _scope_query("Ethereum", None, None) == "Ethereum"


def test_filter_after_drops_articles_published_before_the_alert():
    alert_time = datetime(2026, 8, 28, 14, 16, tzinfo=timezone.utc)
    articles = [
        {"title": "before", "published": datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)},
        {"title": "at", "published": datetime(2026, 8, 28, 14, 16, tzinfo=timezone.utc)},
        {"title": "after", "published": datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)},
    ]
    kept = _filter_after(articles, alert_time)
    assert [a["title"] for a in kept] == ["at", "after"]


def test_filter_after_drops_articles_with_unknown_date():
    alert_time = datetime(2026, 8, 28, 14, 16, tzinfo=timezone.utc)
    articles = [{"title": "no date", "published": None}]
    assert _filter_after(articles, alert_time) == []


def test_filter_after_keeps_everything_when_alert_time_unknown():
    articles = [{"title": "no date", "published": None}]
    assert _filter_after(articles, None) == articles


def test_filter_before_drops_articles_published_after_cutoff():
    cutoff = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
    articles = [
        {"title": "inside window", "published": datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)},
        {"title": "at cutoff", "published": cutoff},
        {"title": "too far ahead", "published": datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)},
    ]
    kept = _filter_before(articles, cutoff)
    assert [a["title"] for a in kept] == ["inside window", "at cutoff"]


def test_filter_before_drops_articles_with_unknown_date():
    cutoff = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
    assert _filter_before([{"title": "no date", "published": None}], cutoff) == []


def test_explain_entry_augments_query_and_drops_stale_articles(tmp_path, monkeypatch):
    # alert at 14:16 -> window is [20:16 same day, 02:16 next day].
    cfg = make_config(tmp_path)

    calls = []

    def fake_fetch_news(query, limit=6):
        calls.append((query, limit))
        alert_time = datetime(2026, 8, 28, 14, 16, tzinfo=timezone.utc)
        return [
            {"title": "old", "source": "", "link": "",
             "published": alert_time + timedelta(hours=3)},  # 17:16 - before the window
            {"title": "fresh", "source": "", "link": "",
             "published": alert_time + timedelta(hours=8)},  # 22:16 - inside the window
        ]

    monkeypatch.setattr(explain, "fetch_news", fake_fetch_news)

    captured = {}

    def fake_chat_completion(**kwargs):
        captured["messages"] = kwargs["messages"]
        return "ok"

    monkeypatch.setattr(explain, "chat_completion", fake_chat_completion)

    entry = {
        "symbol": "Ethereum", "last_return_pct": -1.45, "last_close": 2473.66,
        "sent_at": "2026-08-28T14:16:00+00:00",
    }
    explain.explain_entry(cfg, entry, "Ethereum")

    assert calls == [("Ethereum after:2026-08-28 before:2026-08-29", 100)]
    user_message = captured["messages"][1]["content"]
    assert "fresh" in user_message
    assert "old" not in user_message


def test_explain_entry_drops_articles_from_long_after_a_stale_alert(tmp_path, monkeypatch):
    """The bug this guards against: an alert that sits unprocessed for a long
    time (Explain Alerts wasn't run in a while) must not have today's
    unrelated news pass the filter just because it's technically "after" the
    old alert."""
    cfg = make_config(tmp_path)
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [
        # alert at 10:00 -> window is [16:00, 22:00] the same day.
        {"title": "coverage of the actual old move", "source": "", "link": "",
         "published": datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)},
        {"title": "unrelated news from today", "source": "", "link": "",
         "published": datetime.now(timezone.utc)},
    ])

    captured = {}

    def fake_chat_completion(**kwargs):
        captured["messages"] = kwargs["messages"]
        return "ok"

    monkeypatch.setattr(explain, "chat_completion", fake_chat_completion)

    entry = {
        "symbol": "Ethereum", "last_return_pct": -1.45, "last_close": 2473.66,
        "sent_at": "2026-08-01T10:00:00+00:00",
    }
    explain.explain_entry(cfg, entry, "Ethereum")

    user_message = captured["messages"][1]["content"]
    assert "coverage of the actual old move" in user_message
    assert "unrelated news from today" not in user_message


def test_explain_entry_asks_llm_with_no_headlines_when_nothing_survives_filter(tmp_path, monkeypatch):
    """No skip-and-retry here: the window is fixed relative to the alert, so
    retrying later would search the exact same window and find the exact same
    (lack of) results - the LLM is asked anyway and can say so honestly."""
    cfg = make_config(tmp_path)
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [
        {"title": "too old", "source": "", "link": "",
         "published": datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)}
    ])

    captured = {}

    def fake_chat_completion(**kwargs):
        captured["messages"] = kwargs["messages"]
        return "ok"

    monkeypatch.setattr(explain, "chat_completion", fake_chat_completion)

    entry = {
        "symbol": "Ethereum", "last_return_pct": -1.45, "last_close": 2473.66,
        "sent_at": "2026-08-28T14:16:00+00:00",
    }
    assert explain.explain_entry(cfg, entry, "Ethereum").text == "ok"
    user_message = captured["messages"][1]["content"]
    assert "too old" not in user_message
    assert "No news headlines found" in user_message


def test_is_old_enough_true_once_min_age_reached():
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    entry = {"sent_at": (now - timedelta(hours=6)).isoformat()}
    assert _is_old_enough(entry, min_age_hours=6.0, now=now) is True


def test_is_old_enough_false_before_min_age():
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    entry = {"sent_at": (now - timedelta(hours=5, minutes=59)).isoformat()}
    assert _is_old_enough(entry, min_age_hours=6.0, now=now) is False


def test_is_old_enough_true_when_sent_at_missing():
    assert _is_old_enough({}, min_age_hours=6.0) is True


def test_main_skips_alert_younger_than_min_age_without_spending_tokens(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path, sent_at=datetime.now(timezone.utc) - timedelta(hours=1))
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "42")

    def boom(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(explain, "fetch_news", boom)
    monkeypatch.setattr(explain, "chat_completion", boom)
    monkeypatch.setattr(explain, "edit_telegram_message", boom)

    assert explain.main() == 0
    saved = load_alerts_log(cfg.alerts_log_path)
    assert saved[0]["explained"] is False


def test_main_processes_alert_older_than_min_age(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path, sent_at=datetime.now(timezone.utc) - timedelta(hours=14))
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "42")
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [
        {"title": "headline", "source": "", "link": "", "published": datetime.now(timezone.utc)}
    ])
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "explanation")
    monkeypatch.setattr(explain, "edit_telegram_message", lambda *a, **k: None)

    assert explain.main() == 0
    saved = load_alerts_log(cfg.alerts_log_path)
    assert saved[0]["explained"] is True


def test_select_todo_filters_to_one_message_id():
    entries = [{"message_id": 1, "explained": False}, {"message_id": 2, "explained": False}]
    assert _select_todo(entries, message_id=2) == [entries[1]]


def test_select_todo_still_excludes_already_explained():
    entries = [{"message_id": 1, "explained": True}, {"message_id": 2, "explained": False}]
    assert _select_todo(entries, message_id=1) == []


def seed_two_pending_entries(path, ages_hours=(14, 14)):
    entries = []
    for message_id, symbol, age in [(101, "Ethereum", ages_hours[0]), (202, "Bitcoin", ages_hours[1])]:
        record_sent_alert(
            entries,
            chat_id="@chan",
            message_id=message_id,
            symbol=symbol,
            message_text=f"{symbol} - unusual market move",
            last_close=100.0,
            last_return_pct=-1.0,
            ewma_z=-3.5,
            robust_z=-3.5,
            volume_z=0.5,
            now=datetime.now(timezone.utc) - timedelta(hours=age),
        )
    save_alerts_log(path, entries)


def test_main_with_message_id_env_processes_only_that_entry(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seed_two_pending_entries(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "202")
    monkeypatch.setattr(explain, "fetch_news", lambda query, limit=6: [])
    monkeypatch.setattr(explain, "chat_completion", lambda **kwargs: "explanation")

    edits = []
    monkeypatch.setattr(
        explain, "edit_telegram_message",
        lambda token, chat_id, message_id, text: edits.append(message_id),
    )

    assert explain.main() == 0
    assert edits == [202]
    saved = {e["message_id"]: e["explained"] for e in load_alerts_log(cfg.alerts_log_path)}
    assert saved == {101: False, 202: True}


def test_main_with_unknown_message_id_env_returns_error(tmp_path, monkeypatch, capsys):
    cfg = make_config(tmp_path)
    seed_two_pending_entries(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "999")

    def boom(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(explain, "fetch_news", boom)
    monkeypatch.setattr(explain, "chat_completion", boom)
    monkeypatch.setattr(explain, "edit_telegram_message", boom)

    assert explain.main() == 1
    assert "999" in capsys.readouterr().err


def test_main_with_non_numeric_message_id_env_returns_error(tmp_path, monkeypatch, capsys):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "not-a-number")

    assert explain.main() == 1
    assert "not-a-number" in capsys.readouterr().err


def test_main_with_blank_message_id_env_returns_error(tmp_path, monkeypatch, capsys):
    """A required workflow input GitHub Actions failed to enforce, or an
    empty string passed some other way, must not silently fall back to
    processing everything."""
    cfg = make_config(tmp_path)
    seed_pending_entry(cfg.alerts_log_path)
    monkeypatch.setattr(explain, "load_config", lambda: cfg)
    monkeypatch.setenv("EXPLAIN_MESSAGE_ID", "")

    def boom(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(explain, "fetch_news", boom)
    monkeypatch.setattr(explain, "chat_completion", boom)
    monkeypatch.setattr(explain, "edit_telegram_message", boom)

    assert explain.main() == 1
    assert "Message ID" in capsys.readouterr().err
