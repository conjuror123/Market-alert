"""Set an instrument's size floor from a private Telegram command.

`/floor BKLN 2.5` writes `min_move_sigma: 2.5` on that instrument's entry in
`config/basket.yaml`. `/floor Base metals 2.5` does the same for every
instrument in the block. The number is taken as typed, including when it is
smaller than the floor already there.

WHY THIS EXISTS. The old `--boring` path recorded a verdict and printed a
suggestion; someone still had to edit the yaml. The person being interrupted
is on Telegram, so the lever belongs there, and it has to land before saed
runs in the same hour or the command would only affect tomorrow.

Commands are accepted only in a private chat with the bot (the health chat,
or the product chat when that is itself private). Channel messages are
ignored. The yaml edit is surgical: comments and unrelated keys stay put.
"""
from __future__ import annotations

import logging
import re
import sys

from price_monitor.config import Config, load_config
from price_monitor.notifier import TelegramError, fetch_telegram_updates, send_telegram_message
from price_monitor.state import CorruptState, load_state, save_state
from price_monitor.tremor_delivery import BLOCK_LABEL
from tremor.basket import DEFAULT_BASKET_PATH, load_basket, load_tuning

log = logging.getLogger("price_monitor.floor")

OFFSET_KEY = "telegram_update_offset"

# How a person names a block, including the labels the messages already use.
# "Base metals" is the industrial-metals block as the reader says it, not DBB's
# instrument label alone.
BLOCK_ALIASES = {
    "base metals": "industrial_metals",
    "industrial metals": "industrial_metals",
    "precious metals": "precious_metals",
    "us treasuries": "rates",
    "treasuries": "rates",
    "the dollar block": "FX",
    "dollar block": "FX",
    "currencies": "FX",
    "fx": "FX",
    "us and global equities": "equity",
    "equities": "equity",
    "corporate and sovereign credit": "credit",
}

COMMAND = re.compile(
    r"^/floor(?:@\S+)?\s+(.+?)\s+([0-9]+(?:\.[0-9]+)?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
TICKER_LINE = re.compile(r"^  - ticker: (\S+)\s*(?:#.*)?$")
FLOOR_LINE = re.compile(r"^(    min_move_sigma:\s*)([0-9]+(?:\.[0-9]+)?)(.*)$")
TOP_LEVEL = re.compile(r"^[A-Za-z0-9_]+:")


def _norm(name: str) -> str:
    return " ".join(name.strip().lower().replace("_", " ").split())


def _block_names() -> dict[str, str]:
    names = dict(BLOCK_ALIASES)
    for key, label in BLOCK_LABEL.items():
        names[_norm(key)] = key
        names[_norm(label)] = key
    return names


def resolve_target(name: str, basket=None) -> tuple[str, str, list[str]]:
    """Map a typed name to (kind, label, asset_ids).

    kind is `instrument` or `block`. Raises ValueError if nothing matches.
    """
    basket = basket or load_basket()
    raw = name.strip()
    if not raw:
        raise ValueError("a ticker or a block name is required")
    squashed = raw.upper().replace("/", "")
    for asset in basket.instruments:
        if asset.ticker.upper() == raw.upper() or asset.asset_id.upper() == raw.upper():
            return "instrument", asset.ticker, [asset.asset_id]
        if asset.ticker.upper().replace("/", "") == squashed:
            return "instrument", asset.ticker, [asset.asset_id]

    want = _norm(raw)
    block = _block_names().get(want)
    if block is not None:
        ids = [a.asset_id for a in basket.instruments if a.block == block]
        if not ids:
            raise ValueError(f"{raw!r} is a block with no instruments")
        return "block", BLOCK_LABEL.get(block, block), ids

    labelled = []
    for asset in basket.instruments:
        label = _norm(asset.label)
        if label == want or label.startswith(want + " "):
            labelled.append(asset)
    if len(labelled) == 1:
        asset = labelled[0]
        return "instrument", asset.ticker, [asset.asset_id]
    if len(labelled) > 1:
        tickers = ", ".join(a.ticker for a in labelled)
        raise ValueError(f"{raw!r} matches more than one instrument ({tickers})")
    raise ValueError(f"{raw!r} is not an instrument or a block in the basket")


def format_sigma(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _entry_spans(lines: list[str]) -> list[tuple[str, int, int]]:
    starts = []
    for i, line in enumerate(lines):
        match = TICKER_LINE.match(line.rstrip("\n"))
        if match:
            starts.append((i, match.group(1)))
    spans = []
    for idx, (start, ticker) in enumerate(starts):
        if idx + 1 < len(starts):
            end = starts[idx + 1][0]
        else:
            end = next((j for j in range(start + 1, len(lines))
                        if TOP_LEVEL.match(lines[j])), len(lines))
        spans.append((ticker, start, end))
    return spans


def set_floors(tickers: list[str], value: float,
               path: str = DEFAULT_BASKET_PATH) -> list[str]:
    """Write `min_move_sigma` on each ticker's yaml entry. Returns those edited."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    newline = "\n"
    ended = text.endswith("\n")
    lines = text.splitlines()
    remaining = {t.upper() for t in tickers}
    written: list[str] = []
    shown = format_sigma(value)

    while remaining:
        spans = {ticker.upper(): (ticker, start, end)
                 for ticker, start, end in _entry_spans(lines)}
        missing = [t for t in remaining if t not in spans]
        if missing:
            raise ValueError(f"no yaml entry for {', '.join(sorted(missing))}")
        ticker_key = next(iter(remaining))
        ticker, start, end = spans[ticker_key]
        replaced = False
        last_field = start
        for i in range(start + 1, end):
            match = FLOOR_LINE.match(lines[i])
            if match:
                lines[i] = f"{match.group(1)}{shown}{match.group(3)}"
                replaced = True
                break
            if lines[i].startswith("    ") and not lines[i].lstrip().startswith("#"):
                last_field = i
        if not replaced:
            lines.insert(last_field + 1, f"    min_move_sigma: {shown}")
        written.append(ticker)
        remaining.discard(ticker_key)

    out = newline.join(lines)
    if ended:
        out += newline
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(out)
    load_tuning.cache_clear()
    return written


def parse_command(text: str) -> tuple[str, float] | None:
    """`(name, sigma)` from a chat message, or None if it is not a /floor."""
    if not text:
        return None
    match = COMMAND.match(text.strip())
    if not match:
        return None
    return match.group(1).strip(), float(match.group(2))


def _allowed_chat(chat: dict, cfg: Config) -> bool:
    if (chat or {}).get("type") != "private":
        return False
    cid = str(chat.get("id", ""))
    allowed = {str(cfg.telegram_health_chat_id), str(cfg.telegram_chat_id)}
    allowed.discard("")
    if not allowed:
        return True
    return cid in allowed


def apply_command(name: str, value: float, path: str = DEFAULT_BASKET_PATH,
                  basket=None) -> str:
    if value < 0:
        raise ValueError("min_move_sigma must not be negative")
    kind, label, asset_ids = resolve_target(name, basket)
    basket = basket or load_basket()
    tickers = []
    by_id = {a.asset_id: a for a in basket.instruments}
    for asset_id in asset_ids:
        tickers.append(by_id[asset_id].ticker)
    set_floors(tickers, value, path)
    shown = format_sigma(value)
    if kind == "block":
        listed = ", ".join(tickers)
        return (f"Floor for {label} is now {shown}x "
                f"({listed}).")
    return f"Floor for {label} is now {shown}x."


def process_updates(cfg: Config, state: dict,
                    path: str = DEFAULT_BASKET_PATH) -> int:
    """Read private /floor commands and write them. Returns how many applied."""
    if not cfg.telegram_bot_token:
        return 0
    offset = state.get(OFFSET_KEY)
    try:
        updates = fetch_telegram_updates(cfg.telegram_bot_token, offset)
    except TelegramError as exc:
        log.error("Could not read Telegram commands: %s", exc)
        return 0
    if not updates:
        return 0

    applied = 0
    for update in updates:
        state[OFFSET_KEY] = int(update["update_id"]) + 1
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text") or "")
        parsed = parse_command(text)
        if parsed is None:
            continue
        if not _allowed_chat(chat, cfg):
            log.info("Ignored /floor from chat %s", chat.get("id"))
            continue
        name, value = parsed
        reply_chat = str(chat.get("id") or cfg.telegram_health_chat_id
                         or cfg.telegram_chat_id)
        try:
            reply = apply_command(name, value, path)
            applied += 1
            log.info("%s", reply)
        except ValueError as exc:
            reply = f"Could not set the floor: {exc}"
            log.warning("%s", reply)
        try:
            send_telegram_message(cfg.telegram_bot_token, reply_chat, reply)
        except TelegramError as exc:
            log.error("Could not reply to /floor: %s", exc)
    return applied


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    try:
        state = load_state(cfg.state_path)
    except CorruptState as exc:
        log.error("Refusing to run with a corrupt sent map: %s", exc)
        return 2
    n = process_updates(cfg, state)
    save_state(cfg.state_path, state)
    log.info("Floor commands applied: %d", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
