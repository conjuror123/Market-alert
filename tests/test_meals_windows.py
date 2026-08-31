import pytest

from meals import windows


def test_w_asset_scales_with_the_session_length():
    # Фонд США: 7 баров в сессии -> 840. Валютная пара: 24 -> 2880. Одно и то
    # же число баров означает у них разный отрезок истории, поэтому окно и
    # выводится из B_asset, а не задаётся константой.
    assert windows.w_asset(7) == 840
    assert windows.w_asset(24) == 2880


def test_w_asset_has_a_floor():
    # На инструменте с очень короткой сессией окно не должно схлопнуться.
    assert windows.w_asset(1) == 720
    assert windows.w_asset(6) == 720


def test_w_asset_rejects_a_nonpositive_session():
    with pytest.raises(ValueError):
        windows.w_asset(0)


def test_sigma_lt_takes_all_history_until_the_cap():
    assert windows.sigma_lt_bars(1000) == 1000
    assert windows.sigma_lt_bars(9000) == windows.SIGMA_LT_BARS


def test_sigma_lt_is_undefined_below_the_minimum():
    # "не менее 720 баров" - это не пожелание: на более коротком ряду
    # долгосрочная сигма не определена, и молча подставить что-то нельзя.
    with pytest.raises(ValueError, match="720"):
        windows.sigma_lt_bars(719)


def test_ewma_periods_match_the_specification():
    assert windows.LAMBDA == pytest.approx(2 / 25)
    assert windows.LAMBDA_Q == pytest.approx(2 / 121)
