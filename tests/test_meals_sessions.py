from datetime import date, datetime, timezone

import pytest

from meals import sessions

ANCHOR = "America/New_York"


def utc(y, m, d, h=0):
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


def test_loads_the_generated_table_without_the_calendar_library():
    # Часовой прогон читает CSV и не должен зависеть от exchange_calendars.
    table = sessions.load_sessions()
    assert len(table) > 1900
    assert table[date(2021, 1, 4)].local_open == "09:30"
    assert table[date(2021, 1, 4)].local_close == "16:00"


def test_missing_table_says_how_to_build_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="python -m meals.sessions"):
        sessions.load_sessions(str(tmp_path / "нет.csv"))


def test_half_sessions_are_marked():
    table = sessions.load_sessions()
    # Пятница после Дня благодарения - закрытие в 13:00.
    day = table[date(2021, 11, 26)]
    assert day.is_early_close
    assert day.local_close == "13:00"
    assert len(sessions.half_sessions(table)) == 15


def test_holiday_is_a_weekday_absent_from_the_table():
    table = sessions.load_sessions()
    # Рождество 2023 - понедельник, биржа закрыта.
    assert sessions.is_holiday(date(2023, 12, 25), table)
    # Обычный вторник - не праздник.
    assert not sessions.is_holiday(date(2023, 12, 26), table)
    # Выходные праздниками не считаются: это обычное закрытие недели.
    assert not sessions.is_holiday(date(2023, 12, 23), table)


def test_reference_week_is_exactly_120_hours():
    # П.2.2: длительность недели эталонного календаря ровно 120 часов,
    # праздники из неё не вычитаются.
    opened, closed = sessions.reference_week_bounds(
        datetime(2026, 8, 26, 12, tzinfo=timezone.utc), ANCHOR)
    assert (closed - opened) / 3600 == sessions.REFERENCE_WEEK_HOURS


def test_reference_week_bounds_shift_with_daylight_saving():
    # Границы заданы в локальном времени биржи, поэтому в UTC они разные летом
    # и зимой: 21:00 и 22:00. Хранить их сразу в UTC запрещено п.2.2 - иначе
    # переход на летнее время сдвинул бы неделю относительно рынка.
    summer, _ = sessions.reference_week_bounds(
        datetime(2026, 7, 15, 12, tzinfo=timezone.utc), ANCHOR)
    winter, _ = sessions.reference_week_bounds(
        datetime(2026, 1, 15, 12, tzinfo=timezone.utc), ANCHOR)

    assert datetime.fromtimestamp(summer, tz=timezone.utc).hour == 21
    assert datetime.fromtimestamp(winter, tz=timezone.utc).hour == 22


def test_hours_inside_and_outside_the_reference_week():
    # Среда середины дня - внутри; суббота - снаружи.
    assert sessions.is_reference_hour(utc(2026, 8, 26, 12), ANCHOR)
    assert not sessions.is_reference_hour(utc(2026, 8, 29, 12), ANCHOR)


def test_moment_before_sunday_open_belongs_to_the_previous_week():
    # Воскресенье 20:00 UTC летом - это 16:00 в Нью-Йорке, за час до открытия
    # недели. Час обязан относиться к предыдущей неделе, а не к наступающей.
    sunday_before_open = utc(2026, 8, 30, 20)
    opened, closed = sessions.reference_week_bounds(
        datetime.fromtimestamp(sunday_before_open, tz=timezone.utc), ANCHOR)

    assert opened < sunday_before_open
    assert closed < sunday_before_open + 3600 * 24
    assert not sessions.is_reference_hour(sunday_before_open, ANCHOR)


def test_reference_week_covers_the_holiday_hours_too():
    # 4 июля 2026 - суббота, возьмём Рождество 2026 (пятница, биржа закрыта).
    # Праздничные часы всё равно принадлежат эталонной неделе.
    assert sessions.is_reference_hour(utc(2026, 12, 25, 15), ANCHOR)


def test_table_matches_the_days_the_data_actually_has():
    # Проверка, которая и подтвердила выбор библиотеки: расписание должно
    # совпадать с фактическими барами день в день. Расхождение означает либо
    # ошибку в календаре, либо дыру в данных - и то и другое надо заметить
    # раньше, чем на нём начнут считаться кворум и кросс-секция.
    import pandas as pd

    from meals import bars

    frame = bars.load(bars.store_path(bars.DEFAULT_BARS_DIR, "twelvedata_SPY"))
    observed = set(pd.to_datetime(frame["hour_utc"], unit="s", utc=True).dt.date)
    table = sessions.load_sessions()
    scheduled = {d for d in table if min(observed) <= d <= max(observed)}

    assert observed - scheduled == set()
    assert scheduled - observed == set()
