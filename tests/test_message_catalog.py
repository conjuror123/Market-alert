"""Every Telegram shape, so a copy tweak can be reviewed in one place."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from price_monitor import tremor_delivery as md
from tests.test_tremor_delivery import HOUR, LABELS, SLOT, _cal, event, use_vix, vix_frame
from tremor import routing

_ISO_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}")
_FIRST_LINE = re.compile(r"^(⬜|🟨|🟧|🟥) <b>[^<]+</b> · .+ [+-]\d+\.\d\d%.* · \d+\.\d×σ$")


def _labels():
    return {
        **LABELS,
        "twelvedata:XLF": "Financial sector",
        "twelvedata:DBB": "Base metals (aluminium, zinc, copper)",
        "twelvedata:SPY": "S&P 500",
        "twelvedata:USD/CAD": "US dollar / Canadian dollar",
    }


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
    start = hour - int(19.5 * 365.25 * 24) * HOUR      # the instrument's first bar
    dbb = event(event_id="jump:DBB", asset_id="twelvedata:DBB", block="industrial_metals",
                tier="noticeable", channel="digest", r=-0.0080, sigma_lt=0.0080 / 4.2,
                hour_utc=hour - 4 * HOUR, digest_slot=SLOT, reading="hour",
                since_utc=hour - 4 * HOUR - 9 * 24 * HOUR, since_z=-4.1, record_start=start)
    xlf = event(event_id="jump:XLF", asset_id="twelvedata:XLF", block="equity",
                tier="major", r=-0.0088, sigma_lt=0.0088 / 8.1, hour_utc=hour, reading="hour",
                since_utc=hour - 200 * 24 * HOUR, since_z=-9.6, record_start=start)
    gold = event(event_id="jump:GLD", tier="extreme", r=0.021, sigma_lt=0.021 / 11.4,
                 hour_utc=hour, reading="hour", since_utc=None, record_start=start)
    spy_night = event(event_id="jump:SPY:night", asset_id="twelvedata:SPY", block="equity",
                      tier="high", r=-0.0421, sigma_lt=0.0421 / 6.3, hour_utc=hour,
                      overnight=True, gap_kind="night", reading="night",
                      since_utc=hour - 3 * 366 * 24 * HOUR, since_z=-6.1, record_start=start)
    spy_weekend = dict(spy_night, event_id="jump:SPY:weekend", gap_kind="weekend",
                       reading="weekend")
    cad_weekend = event(event_id="jump:CAD:weekend", asset_id="twelvedata:USD/CAD",
                        block="FX", tier="major", r=0.0144, sigma_lt=0.0144 / 9.0,
                        hour_utc=hour, overnight=True, gap_kind="weekend")
    digest_now = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
    return {
        "digest": md.format_digest([dbb], labels, (SLOT, routing.next_digest_slot(SLOT)), cal,
                                   digest_now)[0],
        "ping": md.format_ping(dbb, labels),
        "push_high_gap_night": md.format_push(spy_night, labels),
        "push_high_gap_weekend": md.format_push(spy_weekend, labels),
        "push_major": md.format_push(xlf, labels, cal),
        "push_major_fx_weekend": md.format_push(cad_weekend, labels),
        "push_extreme": md.format_push(gold, labels),
    }


def test_every_message_shape_follows_the_copy_rules(monkeypatch, tmp_path):
    samples = catalog(monkeypatch)

    # The note carries the regime; nothing else does.
    digest = samples["digest"]
    assert "Fear gauge" in digest and "Added to digest" not in digest
    assert "⬜ <b>DBB</b> · Base metals (aluminium, zinc, copper) -0.80% · 4.2×σ" in digest

    ping = samples["ping"]
    assert ping == ("⬜ <b>DBB</b> · Base metals (aluminium, zinc, copper) -0.80% · 4.2×σ\n"
                    "Added to digest👆🏻👆🏻")

    # The square is the word, so the word is never written; the size is |move|
    # over its half-year sigma, to one decimal, at the end of the first line.
    push = samples["push_major"]
    assert push.splitlines()[0] == "🟧 <b>XLF</b> · Financial sector -0.88% · 8.1×σ"
    assert push.splitlines()[1] == "📉 Rarest hour in 6 months (then 9.6×σ)"
    assert push.splitlines()[2] == "🕐 <b>14.09.2026 17:00 UTC</b>"
    assert "Lagarde" in push

    # Rarest since: how long since the instrument was at least this rare, among
    # readings of its own kind; the note row says it too, the ping does not.
    assert "📉 Rarest hour in 9 days (then 4.1×σ)" in digest
    assert "Rarest" not in ping
    assert samples["push_extreme"].splitlines()[1] == "📈 Rarest hour in 19 years of record"
    assert samples["push_high_gap_night"].splitlines()[1] == (
        "📉 Rarest night in 3 years (then 6.1×σ)")
    assert samples["push_high_gap_weekend"].splitlines()[1].startswith("📉 Rarest weekend in")

    # A gap is said as a move from the last close to the first print.
    assert "SPY</b> · S&amp;P 500 -4.21% at the open · 6.3×σ" in samples["push_high_gap_night"]
    assert "-4.21% at the open after the weekend · 6.3×σ" in samples["push_high_gap_weekend"]
    assert "+1.44% at the weekly open · 9.0×σ" in samples["push_major_fx_weekend"]

    for name, text in samples.items():
        if name != "digest":
            assert "Fear gauge" not in text, name
            assert _FIRST_LINE.match(text.splitlines()[0]), name
        assert not _ISO_STAMP.search(text), name
        for word in ("noticeable", "high ", "major", "extreme", "usual"):
            assert word not in text.lower(), (name, word)
        if name.startswith("push_"):
            assert "Added to digest" not in text

    out = tmp_path / "message_catalog.txt"
    out.write_text(_as_text(samples), encoding="utf-8")
    dest = "/opt/cursor/artifacts/message_catalog.txt"
    if os.path.isdir("/opt/cursor/artifacts"):
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(_as_text(samples))


def _as_text(samples: dict[str, str]) -> str:
    parts = ["# Tremor Telegram catalog\n"]
    for name, text in samples.items():
        parts.append(f"\n===== {name} =====\n")
        parts.append(text)
        parts.append("\n")
    return "".join(parts)
