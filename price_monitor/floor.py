"""Set a size floor from a private Telegram command.

`/floor BKLN 2.5` writes `min_move_sigma: 2.5` on that instrument's entry in
`config/basket.yaml`. `/floor Base metals 2.5` writes the same number on the
block's OWN line only (`block_min_move_sigma`); members keep their own floors.
The number is taken as typed, including when it is smaller than the floor
already there.

WHY THIS EXISTS. The old `--boring` path recorded a verdict and printed a
suggestion; someone still had to edit the yaml. The person being interrupted
is on Telegram, so the lever belongs there, and it has to land before saed
runs in the same hour or the command would only affect tomorrow.

Commands are accepted only in a private chat with the bot (the health chat,
or the product chat when that is itself private). Channel messages are
ignored. The yaml edit is surgical: comments and unrelated keys stay put.

The confirmation names what changed and how often a line at this size has
opened on the stored event table - unique trading days, not raw hours, and
not a Gaussian "1σ = one hour in 3" table.
"""
from __future__ import annotations

import logging
import os
import re
import sys

from price_monitor.config import Config, load_config
from price_monitor.notifier import TelegramError, fetch_telegram_updates, send_telegram_message
from price_monitor.state import CorruptState, load_state, save_state
from price_monitor.tremor_delivery import BLOCK_LABEL
from tremor.basket import DEFAULT_BASKET_PATH, load_basket, load_tuning
from tremor.saed import DEFAULT_EVENTS_PATH

log = logging.getLogger("price_monitor.floor")

OFFSET_KEY = "telegram_update_offset"
YEAR = 365.25 * 86400

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
SECTION_HEAD = re.compile(r"^block_min_move_sigma:\s*(?:#.*)?$")
BLOCK_ENTRY = re.compile(r"^  ([A-Za-z0-9_]+):\s*")
SHARED_FLOOR = re.compile(r"^min_move_sigma:\s*")


def _norm(name: str) -> str:
    return " ".join(name.strip().lower().replace("_", " ").split())


def _block_names() -> dict[str, str]:
    names = dict(BLOCK_ALIASES)
    for key, label in BLOCK_LABEL.items():
        names[_norm(key)] = key
        names[_norm(label)] = key
    return names


def resolve_target(name: str, basket=None) -> tuple[str, str, str]:
    """Map a typed name to (kind, label, key).

    kind is `instrument` or `block`. key is an asset_id or a block name.
    Raises ValueError if nothing matches.
    """
    basket = basket or load_basket()
    raw = name.strip()
    if not raw:
        raise ValueError("a ticker or a block name is required")
    squashed = raw.upper().replace("/", "")
    for asset in basket.instruments:
        if asset.ticker.upper() == raw.upper() or asset.asset_id.upper() == raw.upper():
            return "instrument", asset.ticker, asset.asset_id
        if asset.ticker.upper().replace("/", "") == squashed:
            return "instrument", asset.ticker, asset.asset_id

    want = _norm(raw)
    block = _block_names().get(want)
    if block is not None:
        if not any(a.block == block for a in basket.instruments):
            raise ValueError(f"{raw!r} is a block with no instruments")
        return "block", BLOCK_LABEL.get(block, block), block

    labelled = []
    for asset in basket.instruments:
        label = _norm(asset.label)
        if label == want or label.startswith(want + " "):
            labelled.append(asset)
    if len(labelled) == 1:
        asset = labelled[0]
        return "instrument", asset.ticker, asset.asset_id
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


def _write_yaml(path: str, lines: list[str], ended: bool) -> None:
    out = "\n".join(lines)
    if ended:
        out += "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(out)
    load_tuning.cache_clear()


def set_floors(tickers: list[str], value: float,
               path: str = DEFAULT_BASKET_PATH) -> list[str]:
    """Write `min_move_sigma` on each ticker's yaml entry. Returns those edited."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
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

    _write_yaml(path, lines, ended)
    return written


def set_block_floor(block: str, value: float,
                    path: str = DEFAULT_BASKET_PATH) -> None:
    """Write `block_min_move_sigma` for this block. Members are not touched."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    ended = text.endswith("\n")
    lines = text.splitlines()
    shown = format_sigma(value)
    head = next((i for i, line in enumerate(lines) if SECTION_HEAD.match(line)), None)
    if head is None:
        anchor = next((i for i, line in enumerate(lines) if SHARED_FLOOR.match(line)), None)
        if anchor is None:
            raise ValueError("basket.yaml has no min_move_sigma to hang a block floor on")
        lines[anchor + 1:anchor + 1] = [
            "block_min_move_sigma:",
            f"  {block}: {shown}",
        ]
    else:
        end = next((j for j in range(head + 1, len(lines))
                    if TOP_LEVEL.match(lines[j])), len(lines))
        replaced = False
        for i in range(head + 1, end):
            match = BLOCK_ENTRY.match(lines[i])
            if match and match.group(1) == block:
                comment = re.search(r"\s+#.*$", lines[i])
                lines[i] = f"  {block}: {shown}" + (comment.group(0) if comment else "")
                replaced = True
                break
        if not replaced:
            lines.insert(end, f"  {block}: {shown}")
    _write_yaml(path, lines, ended)


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


def _day_tz_for(asset_id: str, basket) -> "str | None":
    from tremor import blocks, sessions

    if blocks.is_block(asset_id):
        name = blocks.block_name(asset_id)
        templates = {a.session_template for a in basket.assets if a.block == name}
        return sessions.day_tz(templates.pop()) if len(templates) == 1 else None
    for asset in basket.instruments:
        if asset.asset_id == asset_id:
            return sessions.day_tz(asset.session_template)
    return None


def _event_days(hours, tz_name: "str | None") -> int:
    import pandas as pd

    ts = pd.to_datetime(list(hours), unit="s", utc=True)
    if tz_name:
        ts = ts.tz_convert(tz_name)
    return int(ts.normalize().nunique())


def describe_rate(asset_id: str, floor: float,
                  events_path: str = DEFAULT_EVENTS_PATH,
                  basket=None) -> str:
    """How often a line at this size has opened, from stored events.

    Unique trading days, not raw hours. The stored table already keeps one row
    per day; collapsing again is what makes two legs on the same day count as
    one event if a recompute ever left both. Not a Gaussian table, and not
    called pushes unless the rows being counted are push-tier only.
    """
    import pandas as pd

    from tremor import blocks

    line = "a block line" if blocks.is_block(asset_id) else "a line"
    if not os.path.exists(events_path):
        return "No stored events yet, so a rate is not computed."
    try:
        events = pd.read_parquet(events_path)
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not read events for a floor rate: %s", exc)
        return "Stored events could not be read, so a rate is not computed."
    if events.empty or "asset_id" not in events.columns:
        return "No stored events yet, so a rate is not computed."

    mine = events[events["asset_id"].astype(str) == str(asset_id)]
    if mine.empty:
        who = "this block" if blocks.is_block(asset_id) else asset_id.split(":")[-1]
        return f"No stored events for {who} yet, so a rate is not computed."

    if "r" in mine.columns and "sigma_lt" in mine.columns:
        usual = pd.to_numeric(mine["sigma_lt"], errors="coerce")
        move = pd.to_numeric(mine["r"], errors="coerce").abs()
        keep = mine[move.ge(floor * usual) | usual.isna() | (usual <= 0) | (floor <= 0)]
    else:
        keep = mine

    basket = basket or load_basket()
    tz_name = _day_tz_for(asset_id, basket)
    span = max(int(mine["hour_utc"].max()) - int(mine["hour_utc"].min()), 86400)
    years = span / YEAR
    n = _event_days(keep["hour_utc"], tz_name) if not keep.empty else 0
    if n == 0:
        return (f"No {line} in {years:.1f} years of stored history would have "
                f"opened at this floor.")
    per_year = n / years
    if per_year >= 1:
        often = f"about {per_year:.1f} times a year"
    else:
        often = f"about once every {1 / per_year:.1f} years"
    noun = "event" if n == 1 else "events"
    return (f"At this size, {line} has opened {often} "
            f"({n} {noun} over {years:.1f} years of stored history).")


def apply_command(name: str, value: float, path: str = DEFAULT_BASKET_PATH,
                  basket=None, events_path: str = DEFAULT_EVENTS_PATH) -> str:
    if value < 0:
        raise ValueError("min_move_sigma must not be negative")
    kind, label, key = resolve_target(name, basket)
    basket = basket or load_basket()
    shown = format_sigma(value)
    if kind == "block":
        set_block_floor(key, value, path)
        from tremor.blocks import block_id

        rate = describe_rate(block_id(key), value, events_path, basket)
        return (f"Floor for {label} is now {shown}x. Member floors are unchanged.\n"
                f"{rate}")
    ticker = key.split(":")[-1]
    set_floors([ticker], value, path)
    rate = describe_rate(key, value, events_path, basket)
    return f"Floor for {label} is now {shown}x.\n{rate}"


def process_updates(cfg: Config, state: dict,
                    path: str = DEFAULT_BASKET_PATH,
                    events_path: str = DEFAULT_EVENTS_PATH) -> int:
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
            reply = apply_command(name, value, path, events_path=events_path)
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
