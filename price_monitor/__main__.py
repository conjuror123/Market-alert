"""Entry point: run one monitoring pass over all configured assets."""
from __future__ import annotations

import logging
import sys

import requests

from price_monitor.analysis import analyze
from price_monitor.config import load_config
from price_monitor.market_data import fetch_candles
from price_monitor.models import ExchangeError
from price_monitor.notifier import TelegramError, send_telegram_message
from price_monitor.state import is_in_cooldown, load_state, record_alert, save_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price_monitor")


def format_alert(signal) -> str:
    direction_emoji = "🚀" if signal.last_return_pct >= 0 else "🔻"
    lines = [
        f"{direction_emoji} <b>{signal.symbol}</b> — необычное движение рынка",
        f"Цена: {signal.last_close:g} ({signal.last_return_pct:+.2f}% за последний интервал)",
        f"EWMA z-score: {signal.ewma_z:.2f} | Робастный z-score: {signal.robust_z:.2f} | Объём z-score: {signal.volume_z:.2f}",
        "",
    ]
    lines.extend(f"• {r}" for r in signal.reasons)
    return "\n".join(lines)


def main() -> int:
    cfg = load_config()
    state = load_state(cfg.state_path)
    session = requests.Session()

    had_error = False
    alerts_sent = 0

    for asset in cfg.assets:
        state_key = f"{asset.source}:{asset.symbol}"
        try:
            candles = fetch_candles(asset, cfg, session=session)
        except ExchangeError as exc:
            log.error("Failed to fetch data for %s (%s): %s", asset.label, asset.symbol, exc)
            had_error = True
            continue

        signal = analyze(
            symbol=asset.label,
            candles=candles,
            ewma_lambda=cfg.ewma_lambda,
            mad_window=cfg.mad_window,
            price_zscore_threshold=cfg.price_zscore_threshold,
            volume_zscore_threshold=cfg.volume_zscore_threshold,
            volume_min_price_move_z=cfg.volume_min_price_move_z,
            min_history=cfg.min_history,
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

        if is_in_cooldown(state, state_key, cfg.cooldown_minutes):
            log.info("%s: alert condition met but still in cooldown, skipping notification", asset.label)
            continue

        try:
            send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, format_alert(signal))
            record_alert(state, state_key)
            alerts_sent += 1
            log.info("%s: alert sent", asset.label)
        except TelegramError as exc:
            log.error("%s: failed to send Telegram alert: %s", asset.label, exc)
            had_error = True

    save_state(cfg.state_path, state)
    log.info("Run complete. Alerts sent: %d", alerts_sent)
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
