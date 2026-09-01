import json
from datetime import datetime, timezone

import pytest

from meals import calendar_multiplier as cm

HOUR = 3600


def test_matches_the_worked_example_from_the_spec():
    # П.4.3, High, T_event = 12:30 UTC, округление до четвёртого знака.
    assert cm.multiplier_at(-0.5, "High") == pytest.approx(1.7333, abs=5e-5)
    assert cm.multiplier_at(0.5, "High") == pytest.approx(1.6667, abs=5e-5)
    assert cm.multiplier_at(2.5, "High") == pytest.approx(1.1333, abs=5e-5)
    assert cm.multiplier_at(3.5, "High") == 1.0


def test_peak_is_at_the_publication_moment():
    # approx, а не точное равенство: 1 + 0.8 * 6 / 6 в double даёт
    # 1.8000000000000003. Округлять внутри функции было бы неверно - пример в
    # п.4.3 округляет только для печати.
    assert cm.multiplier_at(0.0, "High") == pytest.approx(1.8)
    assert cm.multiplier_at(0.0, "Medium") == pytest.approx(1.5)


def test_function_is_continuous_at_the_window_edges():
    # На обеих границах функция равна единице, поэтому включать границу или нет
    # на результат не влияет.
    for importance in ("High", "Medium"):
        before, after = cm.BEFORE[importance], cm.AFTER[importance]
        assert cm.multiplier_at(-before, importance) == pytest.approx(1.0)
        assert cm.multiplier_at(after, importance) == pytest.approx(1.0)
        assert cm.multiplier_at(-before - 0.01, importance) == 1.0
        assert cm.multiplier_at(after + 0.01, importance) == 1.0


def test_window_before_is_wider_than_after():
    # Асимметрия намеренная: рынок готовится к публикации заранее, а реакция
    # после неё укладывается быстрее.
    assert cm.BEFORE["High"] > cm.AFTER["High"]
    assert cm.multiplier_at(-3, "High") > cm.multiplier_at(3, "High")


def test_low_importance_is_ignored():
    assert cm.multiplier_at(0.0, "Low") == 1.0


def test_series_takes_the_maximum_over_overlapping_events(tmp_path):
    # Два события подряд не делают час вдвое значимее - перемножение раздувало
    # бы индекс в дни, когда публикаций много, а таких дней большинство.
    path = tmp_path / "calendar.ndjson"
    moment = datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc)
    with open(path, "w", encoding="utf-8") as f:
        for importance in ("Medium", "High"):
            f.write(json.dumps({"date": moment.isoformat(), "impact": importance,
                                "country": "USD", "title": "x"}) + "\n")

    close = int(moment.timestamp()) - 1800   # бар, закрывающийся в 12:00
    hour = close - HOUR
    values = cm.multiplier_series([hour], str(path))

    assert values[hour] == pytest.approx(cm.multiplier_at(-0.5, "High"), abs=5e-5)


def test_series_is_one_far_from_any_event(tmp_path):
    path = tmp_path / "calendar.ndjson"
    moment = datetime(2026, 3, 4, 12, 30, tzinfo=timezone.utc)
    path.write_text(json.dumps({"date": moment.isoformat(), "impact": "High",
                                "country": "USD", "title": "x"}) + "\n", encoding="utf-8")

    far = int(datetime(2026, 3, 10, tzinfo=timezone.utc).timestamp())
    assert cm.multiplier_series([far], str(path))[far] == 1.0


def test_window_is_not_shortened_by_a_weekend(tmp_path):
    # Единственное в ТЗ исключение из правила единиц: окна календарного
    # множителя измеряются в КАЛЕНДАРНЫХ часах и через выходные не сокращаются.
    path = tmp_path / "calendar.ndjson"
    friday_evening = datetime(2026, 3, 6, 21, 30, tzinfo=timezone.utc)
    path.write_text(json.dumps({"date": friday_evening.isoformat(), "impact": "High",
                                "country": "USD", "title": "x"}) + "\n", encoding="utf-8")

    two_hours_after = int(friday_evening.timestamp()) + 2 * HOUR - 1800
    hour = two_hours_after - HOUR
    assert cm.multiplier_series([hour], str(path))[hour] > 1.0


def test_missing_calendar_file_gives_no_multiplier(tmp_path):
    assert cm.multiplier_series([HOUR], str(tmp_path / "нет.ndjson")) == {HOUR: 1.0}
