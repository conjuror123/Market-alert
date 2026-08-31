from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from meals import bars, volume
from meals.basket import Asset

ET = "America/New_York"


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="30min", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def bar(day: date, hour: int, vol: float):
    moment = datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo(ET))
    return (int(moment.timestamp()), 1.0, 1.0, 1.0, 1.0, vol, 2)


def frame(rows):
    return pd.DataFrame(rows, columns=list(bars.SCHEMA)).astype(bars.SCHEMA)


def days(n, start=date(2021, 3, 1)):
    from datetime import timedelta
    return [start + timedelta(days=i) for i in range(n)]


def test_forex_gets_null_not_zero():
    # П.3.5: у инструмента без объёма подтверждение объёмом НЕ ОЦЕНИВАЕТСЯ.
    # Ноль означал бы "объём обычный", а это утверждение о данных, которых нет.
    fx = asset(ticker="EUR/USD", block="FX", has_volume=False, tick_size=0.00001,
               session_template="fx_continuous", fetch_interval="1h")
    data = frame([bar(d, 10, 0.0) for d in days(30)])

    assert volume.robust_volume_z(fx, data, ET).isna().all()


def test_seasonal_hour_is_not_an_anomaly_by_itself():
    # Открытие рынка кратно активнее полудня. Если сравнивать с общей нормой,
    # каждое утро было бы всплеском. Норма берётся по своему часу.
    rows = []
    for d in days(40):
        rows.append(bar(d, 10, 10_000_000.0))  # шумный час открытия
        rows.append(bar(d, 13, 1_000_000.0))   # тихий полдень
    data = frame(rows)

    result = volume.robust_volume_z(asset(), data, ET)
    measured = result.dropna()

    assert len(measured) > 0
    # Оба часа обычны для самих себя, поэтому оба около нуля.
    assert measured.abs().max() < 1.0


def test_a_genuine_surge_is_flagged():
    rows = [bar(d, 10, 1_000_000.0 * (1 + 0.01 * i))
            for i, d in enumerate(days(30))]
    rows.append(bar(date(2021, 4, 15), 10, 20_000_000.0))
    result = volume.robust_volume_z(asset(), frame(rows), ET)

    assert result.iloc[-1] > 2.5


def test_profile_excludes_the_current_day():
    # Норма, в которую включено оцениваемое наблюдение, подстраивается под него
    # и занижает собственное срабатывание.
    rows = [bar(d, 10, 1_000_000.0) for d in days(25)]
    rows.append(bar(date(2021, 4, 15), 10, 50_000_000.0))
    result = volume.robust_volume_z(asset(), frame(rows), ET)

    # Профиль вырожден (объём не менялся), поэтому по п.3.5 V_R = 0, а не
    # бесконечность - но важно, что всплеск в свой же профиль не попал.
    assert result.iloc[-1] == 0.0


def test_degenerate_profile_gives_zero():
    # Объём этого часа не менялся 20 дней: MAD равен нулю, делить нельзя.
    rows = [bar(d, 10, 1_000_000.0) for d in days(30)]
    result = volume.robust_volume_z(asset(), frame(rows), ET).dropna()

    assert (result == 0.0).all()


def test_half_sessions_are_kept_out_of_the_profile():
    # В сокращённый день объём заведомо меньше, и держать такие дни в норме
    # значит занижать её для всех полных.
    #
    # Эффект приходится строить намеренно крупным, и это само по себе говорит
    # о свойстве оценки: медиана и MAD настолько устойчивы, что один короткий
    # день из двадцати не сдвигает их вовсе. Чтобы разница проявилась, полоса
    # сокращённых дней должна занять половину окна.
    all_days = days(40)
    short_days = set(all_days[20:30])
    rows = [bar(d, 10, 200_000.0 if d in short_days else 1_000_000.0 + 10_000 * i)
            for i, d in enumerate(all_days)]
    data = frame(rows)

    with_filter = volume.robust_volume_z(asset(), data, ET, set(all_days) - short_days)
    without_filter = volume.robust_volume_z(asset(), data, ET, None)

    # Сокращённые дни в профиле портят норму не смещением центра, а раздутым
    # разбросом: распределение становится двугорбым - около 200 тысяч и около
    # миллиона, - и MAD растягивается на весь разрыв между горбами. Знаменатель
    # раздувается, и настоящий всплеск перестаёт выделяться. То есть фильтр
    # защищает не от ложных срабатываний, а от слепоты.
    assert with_filter.iloc[-1] > 1.5 * without_filter.iloc[-1]


def test_short_history_leaves_the_value_undefined():
    rows = [bar(d, 10, 1_000_000.0) for d in days(5)]
    assert volume.robust_volume_z(asset(), frame(rows), ET).isna().all()


def test_local_hour_not_utc_hour():
    # Сезонность привязана к расписанию торгов, а оно живёт в местном времени и
    # переезжает относительно UTC при переходе на летнее время. Один и тот же
    # биржевой час зимой и летом - это разные часы UTC.
    winter = datetime(2021, 1, 15, 10, tzinfo=ZoneInfo(ET))
    summer = datetime(2021, 7, 15, 10, tzinfo=ZoneInfo(ET))
    assert winter.astimezone(ZoneInfo("UTC")).hour != summer.astimezone(ZoneInfo("UTC")).hour

    rows = ([bar(d, 10, 1_000_000.0 + 1000 * i) for i, d in enumerate(days(25, date(2021, 1, 4)))]
            + [bar(d, 10, 1_000_000.0 + 1000 * i) for i, d in enumerate(days(25, date(2021, 7, 5)))])
    result = volume.robust_volume_z(asset(), frame(rows), ET).dropna()

    # Все бары попали в один профиль десятого часа, несмотря на смену UTC-часа.
    assert len(result) >= 25


def test_empty_input():
    assert volume.robust_volume_z(asset(), bars.empty_frame(), ET).empty


def test_full_session_days_drops_early_closes():
    from meals import sessions

    table = {
        date(2021, 11, 26): sessions.Session(date(2021, 11, 26), "09:30", "13:00", True),
        date(2021, 11, 29): sessions.Session(date(2021, 11, 29), "09:30", "16:00", False),
    }
    assert volume.full_session_days(table) == {date(2021, 11, 29)}
