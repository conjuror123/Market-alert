"""Entry point: run one monitoring pass over all configured assets.

Two independent signals run per asset every hour:
- "hourly": the original signal, one closed hourly candle vs. this asset's
  own recent hourly-return history - catches a sharp move within a single
  hour.
- "daily": resamples this asset's local candle history (candle_store.py) into
  UTC-midnight daily candles and compares the same way against day-scale
  history - catches a slow multi-hour grind that no single hourly return is
  extreme enough to flag on its own, but that adds up to an unusual day (see
  README, "Дневной сигнал"). It's evaluated every run, not once a day: the
  most recent *closed* day's value doesn't change until the next day rolls
  over, so re-evaluating it hourly is harmless - should_notify's cooldown
  naturally keeps this from producing repeat noise, and no separate
  once-a-day scheduling is needed.

Both signals share the same notify/send/log pipeline (_handle_signal) but
keep independent cooldown state (state keys "source:symbol" vs.
"source:symbol:daily") and independent thresholds, so one never suppresses
or is suppressed by the other.
"""
from __future__ import annotations

import logging
import sys

import requests

from price_monitor import candle_store, decision_log, health
from price_monitor.alerts_log import load_alerts_log, record_sent_alert, save_alerts_log
from price_monitor.analysis import Signal, analyze
from price_monitor.config import Config, EffectiveParams, load_config
from price_monitor.market_data import fetch_candles
from price_monitor.models import ExchangeError
from price_monitor.notifier import TelegramError, edit_telegram_message, send_telegram_message
from price_monitor.state import load_state, record_alert, save_state, should_notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor")

# The daily signal is price-only: a full day's trading volume, summed from
# whatever the source reports hourly, doesn't have an established meaning for
# this app yet (see README), so volume is disabled the same way config.yaml
# already disables it for ES=F's broken seasonality - a threshold no real
# z-score will ever reach.
_DAILY_VOLUME_THRESHOLD = 1e9


def format_alert(signal: Signal, params: EffectiveParams, signal_type: str) -> str:
    is_daily = signal_type == "daily"
    price_threshold = params.daily_price_zscore_threshold if is_daily else params.price_zscore_threshold
    direction_emoji = "🚀" if signal.last_return_pct >= 0 else "🔻"
    direction_word = "выросла" if signal.last_return_pct >= 0 else "упала"
    scope = "последние сутки" if is_daily else "последний час"
    title = "необычное дневное движение" if is_daily else "необычное движение рынка"
    signal_label = (
        "дневной (накопленное движение за закрытые сутки)" if is_daily
        else "часовой (резкое движение за один час)"
    )
    lines = [
        f"{direction_emoji} <b>{signal.symbol}</b> — {title}",
        f"Цена: {signal.last_close:g} ({signal.last_return_pct:+.2f}% за {scope})",
        f"Сигнал: {signal_label}",
        "",
    ]

    if signal.price_alert:
        price_severity = max(abs(signal.ewma_z), abs(signal.robust_z))
        lines.append(
            f"Цена {direction_word} сильнее, чем обычно бывает у этого актива: "
            f"отклонение от привычного разброса движений в {price_severity:.1f} раза "
            f"(обычный порог для тревоги — {price_threshold:.1f})."
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
        f"— обычно от -2 до 2, тревога начинается от ±{price_threshold:.1f}",
        f"Робастный z-score (отклонение от долгосрочного ориентира): {signal.robust_z:.2f} "
        f"— обычно от -2 до 2, тревога начинается от ±{price_threshold:.1f}",
    ]
    if not is_daily:
        lines.append(
            f"Объём z-score (отклонение объёма торгов от привычного): {signal.volume_z:.2f} "
            f"— обычно от 0 до 2, тревога начинается от {params.volume_zscore_threshold:.1f}"
        )
    return "\n".join(lines)


def append_id_footer(alert_text: str, message_id: int) -> str:
    """Adds a quiet "ID: <n>" line, so the ID needed to explain this one alert
    later (see explain.py's EXPLAIN_MESSAGE_ID) is visible right in the
    message - Telegram doesn't show message IDs in its UI otherwise. Only
    knowable after sending (Telegram assigns it), hence the separate edit."""
    return f"{alert_text}\n\n<i>ID: {message_id}</i>"


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


def _handle_signal(
    cfg: Config,
    asset,
    params: EffectiveParams,
    signal: Signal,
    signal_type: str,
    state_key: str,
    state: dict,
    alerts_log: list[dict],
) -> bool:
    """Shared notify/send/log pipeline for one already-computed signal
    (hourly or daily - see module docstring). Always appends a decision_log
    row, whether or not an alert fired. Returns True if a Telegram alert was
    actually sent. Raises TelegramError on send/edit failure, same as before.
    """
    if signal_type == "daily":
        price_threshold = params.daily_price_zscore_threshold
        price_override = params.daily_price_zscore_override
        volume_threshold = volume_override = _DAILY_VOLUME_THRESHOLD
        cooldown_minutes = params.daily_cooldown_minutes
        escalation_factor = params.daily_escalation_factor
    else:
        price_threshold = params.price_zscore_threshold
        price_override = params.price_zscore_override
        volume_threshold = params.volume_zscore_threshold
        volume_override = params.volume_zscore_override
        cooldown_minutes = params.cooldown_minutes
        escalation_factor = params.escalation_factor

    notified = False
    if signal.is_alert:
        if should_notify(
            state, state_key, signal.severity, cooldown_minutes, escalation_factor,
            override_severity=price_override,
        ):
            alert_text = format_alert(signal, params, signal_type)
            message_id = send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, alert_text)
            record_alert(state, state_key, signal.severity)

            final_text = append_id_footer(alert_text, message_id)
            try:
                edit_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, message_id, final_text)
            except TelegramError as exc:
                log.warning("%s (%s): alert sent but failed to add ID footer: %s", asset.label, signal_type, exc)
                final_text = alert_text

            record_sent_alert(
                alerts_log,
                chat_id=cfg.telegram_chat_id,
                message_id=message_id,
                symbol=asset.label,
                message_text=final_text,
                last_close=signal.last_close,
                last_return_pct=signal.last_return_pct,
                ewma_z=signal.ewma_z,
                robust_z=signal.robust_z,
                volume_z=signal.volume_z,
                signal_type=signal_type,
            )
            notified = True
            log.info("%s (%s): alert sent (id=%s)", asset.label, signal_type, message_id)
        else:
            log.info(
                "%s (%s): alert condition met but not a big enough escalation during cooldown, skipping",
                asset.label, signal_type,
            )

    decision_path = decision_log.log_path(cfg.decision_log_dir, asset.source, asset.symbol)
    decision_log.append_decision(
        decision_path, signal, signal_type=signal_type, notified=notified,
        price_zscore_threshold=price_threshold, price_zscore_override=price_override,
        volume_zscore_threshold=volume_threshold, volume_zscore_override=volume_override,
    )
    return notified


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

        # Keep the permanent local candle history up to date - only append
        # candles newer than the last one already stored, tracked here in
        # state.json (already loaded/saved every run) rather than re-reading
        # the growing history file just to check its last line.
        history_path = candle_store.store_path(cfg.candle_history_dir, asset.source, asset.symbol)
        candle_state_key = f"{state_key}:last_candle"
        last_open_time = state.get(candle_state_key, {}).get("open_time")
        new_candles = [c for c in candles if last_open_time is None or c.open_time > last_open_time]
        if new_candles:
            candle_store.append_candles(history_path, new_candles)
            state.setdefault(candle_state_key, {})["open_time"] = new_candles[-1].open_time

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
        else:
            log.info(
                "%s: return=%.3f%% ewma_z=%.2f robust_z=%.2f volume_z=%.2f alert=%s",
                asset.label, signal.last_return_pct, signal.ewma_z, signal.robust_z,
                signal.volume_z, signal.is_alert,
            )
            try:
                if _handle_signal(cfg, asset, params, signal, "hourly", state_key, state, alerts_log):
                    alerts_sent += 1
            except TelegramError as exc:
                log.error("%s: failed to send Telegram alert: %s", asset.label, exc)
                had_error = True
                error_details.append(f"{asset.label}: не удалось отправить алерт в Telegram ({exc})")

        # Daily signal - reused from the local history, no extra network call
        # (see module docstring and candle_store.daily_closes). Works even
        # before there's a full daily_mad_window of history: analyze() just
        # returns None until daily_min_history days have accumulated.
        daily_candles = candle_store.daily_closes(candle_store.load_candles(history_path))
        daily_signal = analyze(
            symbol=asset.label,
            candles=daily_candles,
            ewma_lambda=params.daily_ewma_lambda,
            mad_window=params.daily_mad_window,
            price_zscore_threshold=params.daily_price_zscore_threshold,
            volume_zscore_threshold=_DAILY_VOLUME_THRESHOLD,
            volume_min_price_move_z=0.0,
            min_history=params.daily_min_history,
            price_zscore_override=params.daily_price_zscore_override,
            volume_zscore_override=_DAILY_VOLUME_THRESHOLD,
        )
        if daily_signal is None:
            log.info("%s: not enough daily history yet, skipping daily signal", asset.label)
        else:
            log.info(
                "%s (daily): return=%.3f%% ewma_z=%.2f robust_z=%.2f alert=%s",
                asset.label, daily_signal.last_return_pct, daily_signal.ewma_z,
                daily_signal.robust_z, daily_signal.is_alert,
            )
            daily_state_key = f"{state_key}:daily"
            try:
                if _handle_signal(cfg, asset, params, daily_signal, "daily", daily_state_key, state, alerts_log):
                    alerts_sent += 1
            except TelegramError as exc:
                log.error("%s: failed to send Telegram daily alert: %s", asset.label, exc)
                had_error = True
                error_details.append(f"{asset.label}: не удалось отправить дневной алерт в Telegram ({exc})")

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
