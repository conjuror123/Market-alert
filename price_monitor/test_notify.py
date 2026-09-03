"""Sends one fixed Telegram message and exits, independent of any market data or
anomaly-detection logic - so you can verify TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
(and that the bot is actually an admin of the target channel/group) are wired up
correctly, without waiting for a real alert to happen to fire.

Usage:
    python -m price_monitor.test_notify
"""
from __future__ import annotations

import sys

from price_monitor.config import load_config
from price_monitor.notifier import TelegramError, send_telegram_message

MESSAGE = (
    "✅ <b>Market Alert: delivery test</b>\n"
    "If you can see this message, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
    "are configured correctly and the bot can write here."
)


def main() -> int:
    cfg = load_config()
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        print(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set in the environment.",
            file=sys.stderr,
        )
        return 1

    try:
        send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, MESSAGE)
    except TelegramError as exc:
        print(f"Failed to send the test message: {exc}", file=sys.stderr)
        return 1

    print(f"Test message sent to chat_id={cfg.telegram_chat_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
