import os

from price_monitor.config import load_config


def test_the_mute_defaults_to_silent_when_nothing_says_otherwise(tmp_path):
    # A missing config file must not mean "start messaging". Wiring the delivery
    # and deciding to be interrupted by it are separate acts.
    assert load_config(str(tmp_path / "absent.yaml")).tremor_alerts_muted is True


def test_the_yaml_sets_the_mute(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("tremor_alerts_muted: false\n", encoding="utf-8")
    assert load_config(str(path)).tremor_alerts_muted is False


def test_the_environment_overrides_the_yaml(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("tremor_alerts_muted: false\n", encoding="utf-8")
    monkeypatch.setenv("TREMOR_ALERTS_MUTED", "true")
    assert load_config(str(path)).tremor_alerts_muted is True


def test_secrets_never_come_from_the_file(tmp_path, monkeypatch):
    # The repository is public. A token in config.yaml would be committed, so
    # these are read from the environment and nowhere else.
    path = tmp_path / "config.yaml"
    path.write_text("telegram_bot_token: leaked\ntwelvedata_api_key: leaked\n",
                    encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    cfg = load_config(str(path))
    assert cfg.telegram_bot_token == "" and cfg.twelvedata_api_key == ""
