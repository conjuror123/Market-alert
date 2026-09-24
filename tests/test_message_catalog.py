"""Every Telegram shape, so a copy tweak can be reviewed in one place."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from price_monitor import tremor_delivery as md
from tests.test_tremor_delivery import (
    HOUR, LABELS, SLOT, _cal, block_event, event, use_vix, vix_frame,
)
from tremor import routing

_ISO_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}")
_RATE = re.compile(r"≈ .+ \(")


def _labels():
    return {
        **LABELS,
        "twelvedata:XLF": "Financial sector",
        "twelvedata:DBB": "Base metals (aluminium, zinc, copper)",
        "twelvedata:BKLN": "Senior bank loans",
    }


def _history(row, n=12, span_days=400):
    hour = int(row["hour_utc"])
    out = []
    for i in range(n):
        out.append(dict(row, event_id=f"{row.get('event_id', 'e')}_{i}",
                        hour_utc=hour - i * span_days // max(n - 1, 1) * 86400))
    return out


def _vix(monkeypatch):
    use_vix(monkeypatch, vix_frame([
        ((2026, 9, 6), (2026, 9, 7, 15), 15.72),
        ((2026, 9, 7), (2026, 9, 8, 15), 15.80),
        ((2026, 9, 12), (2026, 9, 13, 15), 17.10),
        ((2026, 9, 13), (2026, 9, 14, 15), 17.20),
    ]))


def catalog(monkeypatch) -> dict[str, str]:
    """One rendered example of each message the bot can send."""
    _vix(monkeypatch)
    labels = _labels()
    hour = int(datetime(2026, 9, 14, 17, tzinfo=timezone.utc).timestamp())
    cal = _cal([
        ("2026-09-14T15:30:00+00:00", "USD", "EUR ECB President Lagarde Speaks",
         "High"),
        ("2026-09-16T12:30:00+00:00", "CAD", "CPI m/m", "High"),
    ])
    dbb = event(
        event_id="twelvedata_DBB:1", asset_id="twelvedata:DBB",
        block="industrial_metals", tier="noticeable", channel="digest",
        basis="abnormal", r=-0.0080, e_resid=-0.0071, co_block=-0.0008,
        sigma_lt=0.0080 / 2.7, hour_utc=hour - 4 * HOUR,
        record_since=hour - 14 * 86400, retention_today=0.81,
        retention_settled=1.0, digest_slot=SLOT)
    xlf = event(
        event_id="twelvedata_XLF:1789405200", asset_id="twelvedata:XLF",
        block="equity", tier="major", channel="push", basis="abnormal",
        r=-0.0088, e_resid=-0.0088, co_block=-0.0001, sigma_lt=0.0088 / 2.7,
        hour_utc=hour, record_since=hour - 17 * 86400, retention_today=0.39,
        retention_settled=0.39)
    bkln = event(
        event_id="twelvedata_BKLN:1", asset_id="twelvedata:BKLN",
        block="credit", tier="noticeable", channel="digest", basis="abnormal",
        r=-0.0080, e_resid=-0.0080, co_block=0.0, sigma_lt=0.0080 / 2.7,
        hour_utc=hour - HOUR, record_since=hour - 31 * 86400)
    gold_open = event(
        event_id="twelvedata_GLD:open", asset_id="twelvedata:GLD",
        tier="extreme", channel="push", basis="abnormal", r=0.021,
        e_resid=0.019, co_block=0.002, sigma_lt=0.007,
        hour_utc=hour, record_since=hour - 400 * 86400,
        retention_today=None, retention_settled=None)
    shy = event(
        event_id="twelvedata_SHY:1", asset_id="twelvedata:SHY",
        block="rates", tier="extreme", channel="push", basis="absolute",
        r=0.0013, e_resid=0.0013, co_block=0.0, sigma_lt=0.00013,
        hour_utc=hour, record_since=None, retention_settled=None)
    metals = block_event(
        event_id="block_industrial_metals:1", asset_id="block:industrial_metals",
        block="industrial_metals", tier="major", r=-0.0072, e_resid=-0.0072,
        sigma_lt=0.0036, hour_utc=hour, record_since=hour - 20 * 86400,
        leaders="DBB -0.80%, CPER -0.71%", n_members=3,
        retention_today=None, retention_settled=None)
    fx = block_event(
        event_id="block_FX:1", asset_id="block:FX", block="FX",
        tier="major", r=0.0088, e_resid=0.0088, sigma_lt=0.0008,
        hour_utc=hour, record_since=hour - 90 * 86400,
        leaders="USD/CHF +1.31%, EUR/USD -1.22%", n_members=7)
    spy_open = event(
        event_id="twelvedata_SPY:open", asset_id="twelvedata:SPY",
        block="equity", tier="major", channel="push", basis="absolute",
        r=-0.0421, e_resid=-0.0169, co_block=-0.0252, sigma_lt=0.0047,
        hour_utc=hour, record_since=hour - 1595 * 86400, overnight=True,
        retention_today=0.70, retention_settled=None)
    equity_open = block_event(
        event_id="block_equity:open", hour_utc=hour, r=-0.0341, e_resid=-0.0341,
        sigma_lt=0.0064, record_since=hour - 1603 * 86400, overnight=True,
        tier="high", leaders="XLK -6.90%, IWM -5.92%, QQQ -5.50%, XLY -5.31%",
        retention_today=None, retention_settled=None)
    cad_weekend = event(
        event_id="twelvedata_USD_CAD:open", asset_id="twelvedata:USD/CAD",
        block="FX", tier="major", channel="push", basis="absolute",
        r=0.0144, e_resid=0.0080, co_block=0.0064, sigma_lt=0.0011,
        hour_utc=hour, record_since=hour - 3680 * 86400, overnight=True,
        retention_today=None, retention_settled=None)
    window = routing.digest_window(SLOT)
    digest_now = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
    dbb_hist = _history(dbb)
    xlf_hist = _history(xlf, n=65, span_days=int(5.9 * 365))
    return {
        "digest": md.format_digest(
            [dbb], labels, window, cal, digest_now, dbb_hist)[0],
        "ping_noticeable": md.format_ping(dbb, labels),
        "ping_high": md.format_ping(dict(bkln, tier="high"), labels),
        "push_major_settled": md.format_push(
            xlf, labels, cal, xlf_hist, digest_now),
        "push_extreme_open": md.format_push(
            gold_open, labels, None, _history(gold_open), digest_now),
        "push_absolute_record": md.format_push(
            shy, labels, None, _history(dict(shy, asset_id="twelvedata:SHY")),
            digest_now),
        "block_equity": md.format_push(
            block_event(hour_utc=hour, record_since=hour - 30 * 86400),
            labels, None,
            _history(block_event(hour_utc=hour, record_since=hour - 30 * 86400)),
            digest_now),
        "block_metals": md.format_push(
            metals, labels, None, _history(metals), digest_now),
        "block_fx": md.format_push(fx, labels, None, _history(fx), digest_now),
        "push_overnight_gap": md.format_push(
            spy_open, labels, None, _history(spy_open), digest_now),
        "push_weekend_gap": md.format_push(
            cad_weekend, labels, None, _history(cad_weekend), digest_now),
        "block_overnight_gap": md.format_push(
            equity_open, labels, None, _history(equity_open), digest_now),
        "floor_ticker": (
            "Floor for BKLN is now 2.1x.\n"
            "A line has opened about 1.8 times a year "
            "(11 events over 5.9 years of stored history)."),
        "floor_block": (
            "Floor for US and global equities is now 2.5x. "
            "Member floors are unchanged.\n"
            "A block line has opened about once every 1.4 years "
            "(4 events over 5.9 years of stored history)."),
    }


def test_every_message_shape_follows_the_copy_rules(monkeypatch, tmp_path):
    samples = catalog(monkeypatch)
    assert set(samples) >= {
        "digest", "ping_noticeable", "push_major_settled", "block_equity",
        "block_fx", "floor_ticker",
    }

    digest = samples["digest"]
    assert "Fear gauge" in digest
    assert "1 day ago" in digest
    assert "2 days ago" in digest
    assert "7 days ago" in digest
    assert "Added to digest" not in digest
    assert "DBB noticeable or rarer ≈" in digest
    assert " · -0.80%" not in digest.split("DBB", 1)[-1].split("\n", 1)[0]
    assert "biggest move on its own since" in digest

    ping = samples["ping_noticeable"]
    assert ping.startswith("⬜")
    assert "Added to digest" in ping
    assert "Fear gauge" not in ping
    assert " · Base metals" in ping
    assert " -0.80% (2.7x)" in ping
    assert " · -0.80%" not in ping

    push = samples["push_major_settled"]
    assert "Fear gauge" not in push
    assert "Added to digest" not in push
    assert "XLF major or rarer ≈" in push
    assert "biggest move on its own since 17 day ago" in push
    assert "14-09-2026 17:00 UTC" in push
    assert "2026-09-14" not in push
    assert " · Financial sector -0.88%" in push
    assert " · -0.88%" not in push.splitlines()[0]

    # The overnight gap is said as what it is - a move from the last close to
    # the first print - and never as an hour: "at the open", the usual GAP as
    # the yardstick, and a record that is the last gap this big.
    gap = samples["push_overnight_gap"]
    assert "SPY -4.21% at the open" in gap
    assert "usual overnight gap" in gap and "usual hour" not in gap
    assert "the biggest opening gap since" in gap
    assert "gap on its own" in gap and "block opening" in gap
    block_gap = samples["block_overnight_gap"]
    assert "· -3.41% at the open" in block_gap
    assert "a typical member's usual overnight gap" in block_gap
    assert "the whole block opened together" in block_gap.lower()
    assert "biggest gaps: XLK -6.90%" in block_gap
    assert "trading that hour" not in block_gap

    # A currency pair's gap is the weekend, and is called that.
    fx_gap = samples["push_weekend_gap"]
    assert "+1.44% at the weekly open" in fx_gap
    assert "usual weekend gap" in fx_gap
    assert "the biggest weekend gap since" in fx_gap
    assert "overnight" not in fx_gap

    for name, text in samples.items():
        if name == "digest":
            continue
        assert "Fear gauge" not in text, name
        if "UTC</b>" in text:
            assert not _ISO_STAMP.search(text), name
        if name.startswith("push_") or name.startswith("block_"):
            assert _RATE.search(text) or "in at least" in text, name
            assert "Added to digest" not in text

    out = tmp_path / "message_catalog.txt"
    out.write_text(_as_text(samples), encoding="utf-8")
    dest = "/opt/cursor/artifacts/message_catalog.txt"
    if os.path.isdir("/opt/cursor/artifacts"):
        os.makedirs("/opt/cursor/artifacts", exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(_as_text(samples))


def _as_text(samples: dict[str, str]) -> str:
    parts = ["# Tremor Telegram catalog\n"]
    for name, text in samples.items():
        parts.append(f"\n===== {name} =====\n")
        parts.append(text)
        parts.append("\n")
    return "".join(parts)
