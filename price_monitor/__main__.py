"""Entry point: run one monitoring pass over all configured assets."""
from __future__ import annotations

import logging
import sys

import requests

from price_monitor import health
from price_monitor.alerts_log import load_alerts_log, record_sent_alert, save_alerts_log
from price_monitor.analysis import analyze
from price_monitor.config import load_config
from price_monitor.market_data import fetch_candles
from price_monitor.models import ExchangeError
from price_monitor.notifier import TelegramError, send_telegram_message
from price_monitor.state import load_state, record_alert, save_state, should_notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor")


def format_alert(signal, params) -> str:
    direction_emoji = "🚀" if signal.last_return_pct >= 0 else "🔻"
    direction_word = "выросла" if signal.last_return_pct >= 0 else "упала"
    lines = [
        f"{direction_emoji} <b>{signal.symbol}</b> — необычное движение рынка",
        f"Цена: {signal.last_close:g} ({signal.last_return_pct:+.2f}% за последний интервал)",
        "",
    ]

    if signal.price_alert:
        price_severity = max(abs(signal.ewma_z), abs(signal.robust_z))
        lines.append(
            f"Цена {direction_word} сильнее, чем обычно бывает у этого актива: "
            f"отклонение от привычного разброса движений в {price_severity:.1f} раза "
            f"(обычный порог для тревоги — {params.price_zscore_threshold:.1f})."
        )
    if signal.volume_alert:
        lines.append(
            f"Объём торгов необычно высокий: отклонение в {signal.volume_z:.1f} раза "
            f"больше привычного (обычный порог для тревоги — {params.volume_zscore_threshold:.1f})."
        )

    lines += [
        "",
        "ℹ️ <b>Технические детали</b>",
        f"EWMA z-score (отклонение по недавней волатильности): {signal.ewma_z:.2f} "
        f"— обычно от -2 до 2, тревога начинается от ±{params.price_zscore_threshold:.1f}",
        f"Робастный z-score (отклонение от долгосрочного ориентира): {signal.robust_z:.2f} "
        f"— обычно от -2 до 2, тревога начинается от ±{params.price_zscore_threshold:.1f}",
        f"Объём z-score (отклонение объёма торгов от привычного): {signal.volume_z:.2f} "
        f"— обычно от 0 до 2, тревога начинается от {params.volume_zscore_threshold:.1f}",
    ]
    return "\n".join(lines)


def format_health_down(streak: int, error_details: list[str]) -> str:
    lines = [
        f"⚠️ <b>Мониторинг не работает уже {streak} запуск(ов) подряд</b>",
        "Проверьте вкладку Actions в репозитории — возможно, сломался источник",
        "данных или недействителен токен Telegram.",
        "",
    ]
    lines.extend(f"• {d}" for d in error_details[:10])
    return "\n".join(lines)


def format_health_recovered(streak: int) -> str:
    return f"✅ Мониторинг восстановился после {streak} неудачных запуск(ов) подряд."


def main() -> int:
    cfg = load_config()
    state = load_state(cfg.state_path)
    alerts_log = load_alerts_log(cfg.alerts_log_path)
    session = requests.Session()

    had_error = False
    error_details: list[str] = []
    alerts_sent = 0

    for asset in cfg.assets:
        state_key = f"{asset.source}:{asset.symbol}"
        params = cfg.params_for(asset)
        try:
            candles = fetch_candles(asset, cfg, session=session)
        except ExchangeError as exc:
            log.error("Failed to fetch data for %s (%s): %s", asset.label, asset.symbol, exc)
            had_error = True
            error_details.append(f"{asset.label}: не удалось получить данные ({exc})")
            continue

        signal = analyze(
            symbol=asset.label,
            candles=candles,
            ewma_lambda=params.ewma_lambda,
            mad_window=params.mad_window,
            price_zscore_threshold=params.price_zscore_threshold,
            volume_zscore_threshold=params.volume_zscore_threshold,
            volume_min_price_move_z=params.volume_min_price_move_z,
            min_history=params.min_history,
            price_zscore_override=params.price_zscore_override,
            volume_zscore_override=params.volume_zscore_override,
        )
        if signal is None:
            log.info("%s: not enough history yet, skipping", asset.label)
            continue

        log.info(
            "%s: return=%.3f%% ewma_z=%.2f robust_z=%.2f volume_z=%.2f alert=%s",
            asset.label, signal.last_return_pct, signal.ewma_z, signal.robust_z,
            signal.volume_z, signal.is_alert,
        )

        if not signal.is_alert:
            continue

        if not should_notify(
            state, state_key, signal.severity, params.cooldown_minutes, params.escalation_factor,
            override_severity=params.price_zscore_override,
        ):
            log.info(
                "%s: alert condition met but not a big enough escalation during cooldown, skipping",
                asset.label,
            )
            continue

        try:
            alert_text = format_alert(signal, params)
            message_id = send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, alert_text)
            record_alert(state, state_key, signal.severity)
            record_sent_alert(
                alerts_log,
                chat_id=cfg.telegram_chat_id,
                message_id=message_id,
                symbol=asset.label,
                message_text=alert_text,
                last_close=signal.last_close,
                last_return_pct=signal.last_return_pct,
                ewma_z=signal.ewma_z,
                robust_z=signal.robust_z,
                volume_z=signal.volume_z,
            )
            alerts_sent += 1
            log.info("%s: alert sent", asset.label)
        except TelegramError as exc:
            log.error("%s: failed to send Telegram alert: %s", asset.label, exc)
            had_error = True
            error_details.append(f"{asset.label}: не удалось отправить алерт в Telegram ({exc})")

    if had_error:
        streak = health.record_failure(state)
        if health.should_alert_down(streak, cfg.health_alert_after_failures, cfg.health_reminder_every_failures):
            try:
                send_telegram_message(
                    cfg.telegram_bot_token, cfg.telegram_chat_id, format_health_down(streak, error_details))
                log.info("Monitoring-down alert sent (streak=%d)", streak)
            except TelegramError as exc:
                log.error("Failed to send monitoring-down alert: %s", exc)
    else:
        previous_streak = health.record_success(state)
        if previous_streak >= cfg.health_alert_after_failures:
            try:
                send_telegram_message(
                    cfg.telegram_bot_token, cfg.telegram_chat_id, format_health_recovered(previous_streak))
                log.info("Monitoring-recovered alert sent")
            except TelegramError as exc:
                log.error("Failed to send monitoring-recovered alert: %s", exc)

    save_state(cfg.state_path, state)
    save_alerts_log(cfg.alerts_log_path, alerts_log)
    log.info("Run complete. Alerts sent: %d", alerts_sent)
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
