import os

from price_monitor.config import load_config


def test_the_mute_defaults_to_silent_when_nothing_says_otherwise(tmp_path):
    # A missing config file must not mean "start messaging". Wiring the delivery
    # and deciding to be interrupted by it are separate acts.
    assert load_config(str(tmp_path / "absent.yaml")).jump_alerts_muted is True


def test_the_yaml_sets_the_mute(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("jump_alerts_muted: false\n", encoding="utf-8")
    assert load_config(str(path)).jump_alerts_muted is False


def test_the_environment_overrides_the_yaml(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("jump_alerts_muted: false\n", encoding="utf-8")
    monkeypatch.setenv("JUMP_ALERTS_MUTED", "true")
    assert load_config(str(path)).jump_alerts_muted is True


def test_secrets_never_come_from_the_file(tmp_path, monkeypatch):
    # The repository is public. A token in config.yaml would be committed, so
    # these are read from the environment and nowhere else.
    path = tmp_path / "config.yaml"
    path.write_text("telegram_bot_token: leaked\ntelegram_chat_id: leaked\n",
                    encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_HEALTH_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    cfg = load_config(str(path))
    assert cfg.telegram_bot_token == "" and cfg.telegram_chat_id == ""
    assert cfg.telegram_health_chat_id == ""


def test_health_never_falls_back_to_the_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@channel")
    monkeypatch.delenv("TELEGRAM_HEALTH_CHAT_ID", raising=False)
    cfg = load_config(str(tmp_path / "absent.yaml"))
    assert cfg.telegram_chat_id == "@channel"
    assert cfg.telegram_health_chat_id == ""


def test_health_chat_is_its_own_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@channel")
    monkeypatch.setenv("TELEGRAM_HEALTH_CHAT_ID", "12345")
    cfg = load_config(str(tmp_path / "absent.yaml"))
    assert cfg.telegram_chat_id == "@channel"
    assert cfg.telegram_health_chat_id == "12345"
