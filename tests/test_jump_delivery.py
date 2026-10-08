"""How a Jump message reads: the push, the note, the ping, the calendar and
fear-gauge lines. What goes on the channel, and when, is tests/test_delivery_week.py."""
from datetime import datetime, timedelta, timezone

import pandas as pd

from price_monitor import jump_delivery as md
from jump import routing

HOUR = 3600
NOW = datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)
LABELS = {"twelvedata:GLD": "Gold", "coinbase:BTC-USD": "Bitcoin"}

# The note that is open at NOW.
SLOT = routing.digest_slot(int(NOW.timestamp()))


def event(**over):
    """A jump event as jump.jumps.for_delivery writes it: |r| / sigma_lt = 7.0."""
    base = dict(event_id="e1", asset_id="twelvedata:GLD", block="precious_metals",
                hour_utc=int(NOW.timestamp()) - HOUR,
                tier="major", basis="jump", channel="push", r=0.021,
                sigma_lt=0.003, overnight=False, digest_slot=None)
    return base | over


def _cal(rows):
    """rows: (iso date, country, title, impact)."""
    return [{"date": d, "country": c, "title": t, "impact": i,
             "actual": "", "forecast": "", "previous": ""} for d, c, t, i in rows]


def test_a_push_names_the_scheduled_news_behind_it():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "USD", "Core CPI m/m", "High"),
                ("2026-06-10T13:00:00+00:00", "USD", "Fed Chair Speaks", "High")])
    out = md.calendar_context(hour, cal)
    assert out.startswith("Nearby economic events (-2h+1h):")
    assert "USD Core CPI m/m" in out and "USD Fed Chair Speaks" in out


def test_a_push_with_nothing_scheduled_prints_no_calendar_line_at_all():
    # 55% of pushes have no Medium or High release in the window, and a line
    # that usually says nothing stops being read. The absence says it.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    elsewhere = _cal([("2026-05-01T12:00:00+00:00", "USD", "Old CPI", "High")])
    assert md.calendar_context(hour, elsewhere) == ""


def test_an_empty_archive_claims_nothing_rather_than_claiming_silence():
    # An empty archive cannot tell "nothing was scheduled" from "nothing was
    # loaded". Now that a quiet window prints nothing either, the two agree on
    # the output - but for different reasons, and this is the one that would
    # have to change first if the line ever came back.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    assert md.calendar_context(hour, []) == ""


def test_low_impact_news_is_not_named():
    # High and Medium are shown, the same two the Saturday calendar shows. Low
    # is dominated by bank holidays and minor prints, and naming those would
    # turn the most important line of the most important message into noise.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    only_low = _cal([("2026-06-10T12:30:00+00:00", "CHF", "Bank Holiday", "Low")])
    assert md.calendar_context(hour, only_low) == ""

    medium = _cal([("2026-06-10T12:45:00+00:00", "EUR", "Trade Balance", "Medium")])
    assert "Trade Balance" in md.calendar_context(hour, medium)


def test_each_named_release_carries_its_impact_colour():
    # The same circles the Saturday calendar uses, against the squares a move
    # carries: the shape says which kind of thing the line is.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "USD", "CPI", "High"),
                ("2026-06-10T12:45:00+00:00", "EUR", "Trade Balance", "Medium")])
    out = md.calendar_context(hour, cal)
    assert "\U0001F534 \U0001F1FA\U0001F1F8 USD CPI" in out
    assert "\U0001F7E0 \U0001F1EA\U0001F1FA EUR Trade Balance" in out


def test_a_release_carries_its_country_flag_beside_the_code():
    # The flag is what is caught at a glance; the code is what makes it certain.
    # Several of these flags are the same two colours in nearly the same
    # arrangement at the size a phone draws them.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "AUD", "Employment Change", "High"),
                ("2026-06-10T12:40:00+00:00", "NZD", "Official Cash Rate", "High"),
                ("2026-06-10T12:50:00+00:00", "All", "G7 Meetings", "High")])
    out = md.calendar_context(hour, cal)
    assert "\U0001F1E6\U0001F1FA AUD" in out
    assert "\U0001F1F3\U0001F1FF NZD" in out
    # No country at all is the source's own answer, and a globe is the honest
    # rendering of it rather than a stand-in for a missing flag.
    assert "\U0001F310 All" in out


def test_a_currency_with_no_flag_still_prints_its_code():
    # The source can add a currency whenever it likes and the message must not
    # sprout a placeholder box when it does.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "XYZ", "Rate Decision", "High")])
    out = md.calendar_context(hour, cal)
    assert "XYZ Rate Decision" in out


def test_news_outside_the_window_is_not_claimed_as_context():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T09:00:00+00:00", "USD", "Old News", "High"),
                ("2026-06-10T16:30:00+00:00", "USD", "Much Later News", "High")])
    out = md.calendar_context(hour, cal)
    assert "Old News" not in out and "Much Later News" not in out


def test_a_release_inside_the_moved_hour_is_named():
    # hour_utc is the bar's start: a release at 14:30 falls inside the 14:00
    # bar's move, and is the cause a reader would blame first.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T14:30:00+00:00", "USD", "FOMC Statement", "High")])
    assert "FOMC Statement" in md.calendar_context(hour, cal)


def test_the_window_runs_from_two_hours_before_the_bar_to_its_close():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    edges = {"2026-06-10T11:59:00+00:00": False, "2026-06-10T12:00:00+00:00": True,
             "2026-06-10T15:00:00+00:00": True, "2026-06-10T15:01:00+00:00": False}
    for when, named in edges.items():
        cal = _cal([(when, "USD", "CPI m/m", "High")])
        assert ("CPI m/m" in md.calendar_context(hour, cal)) is named, when


def test_a_crowded_window_is_listed_in_full():
    # The High filter is what keeps the line short. On a busy morning the tail
    # is the half worth reading, so it is not traded away to save two lines.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([(f"2026-06-10T12:{m:02d}:00+00:00", "USD", f"Print {m}", "High")
                for m in range(0, 60, 10)])
    out = md.calendar_context(hour, cal)
    assert out.count("\U0001F534") == 6
    assert "more" not in out
    for m in range(0, 60, 10):
        assert f"Print {m}" in out


def test_the_events_are_listed_in_the_order_they_happened():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T14:30:00+00:00", "USD", "Later", "High"),
                ("2026-06-10T12:30:00+00:00", "USD", "Earlier", "High")])
    out = md.calendar_context(hour, cal)
    assert out.index("Earlier") < out.index("Later")


def test_a_missing_calendar_never_costs_the_alert():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    assert md.calendar_context(hour, None) == ""
    text = md.format_push({"hour_utc": hour, "asset_id": "a:SPY", "tier": "major",
                           "basis": "jump", "r": 0.02, "sigma_lt": 0.004}, {}, None)
    assert text.splitlines()[0].endswith("+2.00% · 5.0×σ")


def test_the_comparison_is_skipped_when_the_yardstick_is_missing():
    # sigma_LT is NaN through an instrument's first 720 bars, and a live event
    # there must still render rather than raise.
    event = {"asset_id": "twelvedata:SPY", "tier": "major", "basis": "jump",
             "hour_utc": 1767225600, "r": 0.02, "sigma_lt": float("nan")}
    text = md.format_push(event, {})
    assert "×σ" not in text and "+2.00%" in text


def test_a_move_carries_its_tier_as_a_colour():
    # Squares for moves against the calendar's circles, so the shape says which
    # kind of thing a coloured line is before the words do.
    for tier, square in md.TIER_EMOJI.items():
        assert md.describe(event(tier=tier), LABELS).startswith(square)


def test_the_headline_leads_with_the_rarity_the_ticker_and_the_move():
    # The rarity is a colour so it reads before any word does; the ticker is
    # what a reader types into a chart; the move is the number they came for.
    text = md.describe(event(asset_id="twelvedata:GLD"), LABELS)
    first = text.split("\n")[0]
    assert first == md.TIER_EMOJI["major"] + " <b>GLD</b> · Gold +2.10% · 7.0×σ"


def test_the_hour_is_the_last_line_and_is_bold():
    # Everything above it is what happened; this is when. Bold because it is the
    # one thing a reader cross-checks against a chart.
    text = md.describe(event(asset_id="twelvedata:GLD"), LABELS)
    last = text.split("\n")[-1]
    assert last.startswith(md.TIME_EMOJI)
    stamp = (NOW - timedelta(hours=1)).strftime("%d.%m.%Y %H:%M")
    assert last.endswith("UTC</b>") and f"<b>{stamp}" in last


def vix_frame(rows, spikes=()):
    """rows: (observed date, known date, close). `spikes` indexes into rows."""
    frame = pd.DataFrame({
        "day": [int(datetime(*d, tzinfo=timezone.utc).timestamp()) for d, _, _ in rows],
        "available_at": [int(datetime(*k, tzinfo=timezone.utc).timestamp())
                         for _, k, _ in rows],
        "close": [c for _, _, c in rows],
    })
    frame["is_spike"] = [i in spikes for i in range(len(rows))]
    return frame


def use_vix(monkeypatch, frame):
    monkeypatch.setattr(md, "_vix_scored", lambda: frame)


def test_the_comparison_names_yesterday_and_a_week_ago(monkeypatch):
    use_vix(monkeypatch, vix_frame([
        ((2026, 9, 1), (2026, 9, 2, 15), 14.32),
        ((2026, 9, 4), (2026, 9, 7, 15), 15.10),
        ((2026, 9, 10), (2026, 9, 11, 15), 17.84),
    ]))
    at = int(datetime(2026, 9, 12, 9, tzinfo=timezone.utc).timestamp())
    text = md.vix_context(at)
    assert "Fear gauge VIX" in text
    assert "17.84 1 day ago" in text
    assert "15.10 2 days ago" in text
    assert "14.32 7 days ago" in text
    assert "close" not in text
    assert "7 Sep" not in text


def test_the_comparison_still_prints_the_previous_close_when_it_is_flat(monkeypatch):
    rows = [((2026, 9, 1), (2026, 9, 2, 15), 20.00),
            ((2026, 9, 10), (2026, 9, 11, 15), 14.00)]
    use_vix(monkeypatch, vix_frame(rows))
    at = int(datetime(2026, 9, 12, 9, tzinfo=timezone.utc).timestamp())
    assert "14.00 1 day ago" in md.vix_context(at)
    assert "20.00 2 days ago" in md.vix_context(at)

    rows[1] = ((2026, 9, 10), (2026, 9, 11, 15), 20.05)
    use_vix(monkeypatch, vix_frame(rows))
    assert "20.05 1 day ago" in md.vix_context(at)
    assert "20.00 2 days ago" in md.vix_context(at)


def test_the_gauge_moves_on_as_soon_as_a_reading_is_published(monkeypatch):
    # A live note is re-rendered every run, so it must not sit on a stale gauge:
    # the moment FRED publishes the next close, the line follows it.
    use_vix(monkeypatch, vix_frame([
        ((2026, 9, 1), (2026, 9, 2, 15), 14.32),
        ((2026, 9, 9), (2026, 9, 10, 15), 16.46),
        ((2026, 9, 10), (2026, 9, 11, 15), 17.84),
    ]))
    before = int(datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp())
    after = int(datetime(2026, 9, 11, 16, tzinfo=timezone.utc).timestamp())
    assert "16.46 1 day ago" in md.vix_context(before)
    assert "17.84 1 day ago" in md.vix_context(after)


def test_the_regime_line_never_quotes_a_reading_that_did_not_exist_yet(monkeypatch):
    # FRED publishes VIX one to two business days late. A message about Monday's
    # move that quoted Monday's close would be reading a number the system could
    # not have had, which is the one thing a replayable record must not do.
    use_vix(monkeypatch, vix_frame([
        ((2020, 3, 9), (2020, 3, 10, 15), 54.46),
        ((2020, 3, 10), (2020, 3, 11, 15), 47.30),
        ((2020, 3, 11), (2020, 3, 12, 15), 53.90),
        ((2020, 3, 12), (2020, 3, 13, 15), 75.47),
    ]))
    text = md.vix_context(int(datetime(2020, 3, 12, 19, tzinfo=timezone.utc).timestamp()))

    assert "53.90" in text and "1 day ago" in text
    assert "75.47" not in text


def test_the_regime_line_is_silent_before_any_reading_is_known(monkeypatch):
    use_vix(monkeypatch, vix_frame([((2026, 9, 3), (2026, 9, 4, 15), 14.32)]))
    assert md.vix_context(int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())) == ""


def test_the_level_is_placed_in_its_own_history(monkeypatch):
    # 16 and 54 are both just numbers until one of them is "calmer than three
    # days in five" and the other "higher than all but one day in a hundred".
    rows = [((2020, 1, d // 24 + 1, d % 24), (2020, 2, 1, 15), 10.0 + d)
            for d in range(0, 40)]
    use_vix(monkeypatch, vix_frame(rows))
    at = int(datetime(2020, 3, 1, tzinfo=timezone.utc).timestamp())
    assert "higher than 100% of days" not in md.vix_context(at)
    assert "the highest it has been since" in md.vix_context(at)


def test_a_stress_episode_is_named_while_it_is_running_and_not_after(monkeypatch):
    # A VIX spike opens a stress episode for twenty-four REFERENCE hours, and
    # the weekly note names it while it runs.
    use_vix(monkeypatch, vix_frame([
        ((2020, 2, 24), (2020, 2, 25, 15), 25.0),
        ((2020, 2, 27), (2020, 2, 28, 15), 39.16),
    ], spikes=(1,)))

    inside = md.vix_context(int(datetime(2020, 2, 28, 18, tzinfo=timezone.utc).timestamp()))
    assert "stress episode" in inside and "28.02.2020" in inside

    later = md.vix_context(int(datetime(2020, 3, 20, 18, tzinfo=timezone.utc).timestamp()))
    assert "39.16" in later                      # still the latest known reading
    assert "stress episode" not in later         # but the window closed long ago


def test_a_push_does_not_carry_the_regime_the_note_does(monkeypatch):
    use_vix(monkeypatch, vix_frame([((2026, 9, 3), (2026, 9, 4, 15), 14.32)]))
    later = event(hour_utc=int(datetime(2026, 9, 8, 14, tzinfo=timezone.utc).timestamp()))
    push = md.format_push(later, LABELS)
    assert "Fear gauge" not in push

    window = (int(datetime(2026, 9, 8, 9, tzinfo=timezone.utc).timestamp()),
              int(datetime(2026, 9, 11, 9, tzinfo=timezone.utc).timestamp()))
    note = md.format_digest([event()], LABELS, window,
                            now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc))
    assert "Fear gauge" in note[0] and "14.32" in note[0]


def test_a_ticker_ping_puts_percent_and_size_after_the_name():
    text = md.format_ping(
        event(event_id="p", tier="noticeable", channel="digest",
              asset_id="twelvedata:BKLN", r=-0.008, sigma_lt=0.008 / 2.7),
        {"twelvedata:BKLN": "Senior bank loans"})
    assert text == (
        "⬜ <b>BKLN</b> · Senior bank loans -0.80% · 2.7×σ\n"
        "Added to digest👆🏻👆🏻")
    assert " · -0.80%" not in text


def header_for(y, m, d):
    """The header of the note open on that day."""
    opens = routing.digest_slot(int(datetime(y, m, d, 12, tzinfo=timezone.utc).timestamp()))
    return md.format_digest([], LABELS, (opens, routing.next_digest_slot(opens)),
                            None, NOW)[0].splitlines()[0]


def test_the_header_runs_from_one_weeks_last_close_to_the_next():
    assert "18.09.2026 to 25.09.2026" in header_for(2026, 9, 22)


def test_the_note_header_uses_day_month_year_on_both_ends():
    across = header_for(2026, 3, 31)     # Good Friday week: 27 March to Thursday 2 April
    assert "27.03.2026 to 02.04.2026" in across


def test_the_note_runs_in_time_order_across_all_its_parts():
    # A long note is cut into several messages. Sorting each part on its own
    # would restart the clock at every cut, so the rows are ordered once and the
    # cut falls wherever the character budget runs out.
    rows = [event(event_id=f"d{i}", channel="digest", tier="noticeable",
                  hour_utc=SLOT + i * HOUR, asset_id="twelvedata:GLD")
            for i in range(60)]
    texts = md.format_digest(rows, LABELS, (SLOT, SLOT + 200 * HOUR), None, NOW)
    assert len(texts) > 1, "the fixture must be long enough to split"

    stamps = []
    for part in texts:
        for line in part.split("\n"):
            if line.startswith(md.TIME_EMOJI):
                stamps.append(line)
    assert stamps == sorted(stamps), "the hours must ascend across the parts"
    assert len(stamps) == len(rows)


def test_the_note_stands_in_blocks_in_the_baskets_order_each_under_its_name_line():
    # What moved together is read together: the rates first, then the FX,
    # whatever the hour; inside a block, time order.
    rows = [event(event_id="fx", channel="digest", tier="noticeable", block="FX",
                  asset_id="twelvedata:EUR/USD", hour_utc=SLOT + 1 * HOUR),
            event(event_id="tlt", channel="digest", tier="noticeable", block="rates",
                  asset_id="twelvedata:TLT", hour_utc=SLOT + 3 * HOUR),
            event(event_id="ief", channel="digest", tier="noticeable", block="rates",
                  asset_id="twelvedata:IEF", hour_utc=SLOT + 2 * HOUR)]
    labels = {"twelvedata:EUR/USD": "Euro", "twelvedata:TLT": "Long", "twelvedata:IEF": "Mid"}
    text = md.format_digest(rows, labels, (SLOT, SLOT + 200 * HOUR), None, NOW)[0]
    rates, fx = "━━━ 🏛 <b>RATES</b> ━━━", "━━━ 💱 <b>FX</b> ━━━"
    assert text.index(rates) < text.index("Mid") < text.index("Long") < text.index(fx) \
        < text.index("Euro")
    assert "CREDIT" not in text                     # a block with nothing is left out


def test_a_part_that_opens_inside_a_block_names_it_continued():
    rows = [event(event_id=f"d{i}", channel="digest", tier="noticeable",
                  hour_utc=SLOT + i * HOUR, asset_id="twelvedata:GLD")
            for i in range(60)]
    texts = md.format_digest(rows, LABELS, (SLOT, SLOT + 200 * HOUR), None, NOW)
    assert len(texts) > 1, "the fixture must be long enough to split"
    assert "━━━ 🥇 <b>PRECIOUS METALS</b> ━━━" in texts[0]
    for part in texts[1:]:
        assert part.startswith("━━━ 🥇 <b>PRECIOUS METALS</b> · continued ━━━")
    for part in texts[:-1]:
        assert "━━━" not in part.rstrip().splitlines()[-1]   # never a name-line alone


def test_two_moves_in_one_hour_put_the_rarer_first():
    # The one case time cannot separate.
    same = SLOT + 5 * HOUR
    rows = [event(event_id="mild", channel="digest", tier="noticeable",
                  hour_utc=same, asset_id="twelvedata:GLD"),
            event(event_id="rare", channel="digest", tier="high",
                  hour_utc=same, asset_id="coinbase:BTC-USD")]
    text = md.format_digest(rows, LABELS, (SLOT, SLOT + 200 * HOUR), None, NOW)[0]
    assert text.index("Bitcoin") < text.index("Gold")

    # and reversing the input does not change the answer
    text = md.format_digest(rows[::-1], LABELS, (SLOT, SLOT + 200 * HOUR), None, NOW)[0]
    assert text.index("Bitcoin") < text.index("Gold")


def jump(**over):
    return event(tier="high", r=0.012, sigma_lt=0.002) | over


def test_a_jump_says_its_size_in_sigma_on_the_first_line_and_no_word():
    # The colour of the square is the word; the size is |move| / half-year σ.
    text = md.format_push(jump(), LABELS)
    lines = text.splitlines()
    assert lines[0] == "🟨 <b>GLD</b> · Gold +1.20% · 6.0×σ"
    assert "high" not in text and "usual" not in text


def test_a_jump_is_dated_day_dot_month_dot_year():
    text = md.format_push(jump(), LABELS)
    assert text.splitlines()[-1] == "🕐 <b>12.09.2026 02:00 UTC</b>"


def test_a_jump_ping_says_its_size_in_sigma():
    ping = md.format_ping(jump(tier="noticeable", r=0.0106), LABELS)
    assert ping.splitlines()[0] == "⬜ <b>GLD</b> · Gold +1.06% · 5.3×σ"


def test_a_gap_jump_names_no_yardstick_either():
    # Hour or gap shows in the rarest-since line, not here.
    text = md.format_push(jump(overnight=True, gap_kind="weekend"), LABELS)
    assert text.splitlines()[0].endswith(" · 6.0×σ")
    assert "usual" not in text


# --- the rarest-since line ---------------------------------------------------

def rare(**over):
    return jump(reading="hour", record_start=int(NOW.timestamp()) - 6 * 366 * 24 * HOUR,
                since_utc=None, since_z=None) | over


def test_the_rarest_line_says_how_long_and_how_big_the_last_one_was():
    hour = int(NOW.timestamp()) - HOUR
    text = md.format_push(rare(since_utc=hour - 200 * 24 * HOUR, since_z=7.1), LABELS)
    assert text.splitlines()[1] == "📈 Rarest hour in 6 months (then 7.1×σ)"


def test_a_record_says_how_much_record_there_is():
    text = md.format_push(rare(r=-0.012), LABELS)
    assert text.splitlines()[1] == "📉 Rarest hour in 6 years of record"


def test_the_span_rounds_down_so_the_claim_stays_true():
    day = 24 * HOUR
    assert md._span(1.5 * HOUR) == "1 hour"
    assert md._span(47 * HOUR) == "47 hours"
    assert md._span(59.9 * day) == "59 days"
    assert md._span(60 * day) == "1 month"
    assert md._span(730 * day) == "23 months"
    assert md._span(2.9 * 365.25 * day) == "2 years"


def test_a_gap_names_its_own_kind():
    hour = int(NOW.timestamp()) - HOUR
    night = rare(reading="night", overnight=True, gap_kind="night",
                 since_utc=hour - 3 * 24 * HOUR, since_z=6.0)
    assert md.format_push(night, LABELS).splitlines()[1] == "📈 Rarest night in 3 days (then 6.0×σ)"


def test_no_stage_3_columns_no_line():
    assert "Rarest" not in md.format_push(jump(), LABELS)


# --- the check on the time line -----------------------------------------------

def checked(**over):
    found = int(datetime(2026, 9, 8, 15, tzinfo=timezone.utc).timestamp())
    return jump(hour_utc=found - HOUR, found_utc=found,
                check_utc=int(datetime(2026, 9, 8, 20, tzinfo=timezone.utc).timestamp()),
                held=None) | over


def test_the_time_line_counts_down_to_the_close_in_hours():
    now = int(datetime(2026, 9, 8, 15, 5, tzinfo=timezone.utc).timestamp())
    assert md.check_suffix(checked(now_utc=now)) == " · close in 5h"


def test_a_close_on_a_later_day_is_the_next_close():
    later = int(datetime(2026, 9, 9, 20, tzinfo=timezone.utc).timestamp())
    now = int(datetime(2026, 9, 8, 20, 5, tzinfo=timezone.utc).timestamp())
    assert md.check_suffix(checked(check_utc=later, now_utc=now)) == " · next close in 24h"


def test_the_answer_is_the_share_still_there():
    assert md.check_suffix(checked(held=0.8)) == " · close 80%"
    assert md.check_suffix(checked(held=1.2)) == " · close 120%"
    assert md.check_suffix(checked(held=-0.2)) == " · close -20%"


def test_past_the_close_without_its_bars_it_waits():
    now = int(datetime(2026, 9, 8, 21, 5, tzinfo=timezone.utc).timestamp())
    assert md.check_suffix(checked(now_utc=now)) == " · close pending"


def test_no_check_columns_no_suffix():
    assert md.check_suffix(jump()) == ""
