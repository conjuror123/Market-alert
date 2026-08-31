import numpy as np
import pandas as pd
import pytest

from meals import residuals, windows
from meals.basket import Asset

HOUR = 3600


def asset(**over):
    base = dict(ticker="SPY", source="twelvedata", tier=1, block="equity",
                has_volume=True, tick_size=0.01, session_template="us_equity",
                fetch_interval="1h", label="S&P 500", in_basket=True)
    return Asset(**(base | over))


def frame_with(returns, closes=None):
    n = len(returns)
    return pd.DataFrame({
        "hour_utc": [(i + 1) * HOUR for i in range(n)],
        "close": closes if closes is not None else [100.0] * n,
        "r": returns,
    })


def test_beta_recovers_a_known_exposure():
    rng = np.random.default_rng(0)
    factor = pd.Series(rng.normal(0, 0.01, 600))
    returns = 1.5 * factor + rng.normal(0, 1e-6, 600)

    estimates = residuals.rolling_beta(returns, factor)
    assert estimates["beta"].dropna().iloc[-1] == pytest.approx(1.5, abs=0.01)


def test_beta_is_estimated_out_of_sample():
    # Оценка на баре t построена по данным ДО него: иначе движение, которое мы
    # хотим задетектировать, само подправило бы коэффициент и частично
    # вычлось бы из себя.
    rng = np.random.default_rng(1)
    factor = pd.Series(rng.normal(0, 0.01, 400))
    returns = pd.Series(1.0 * factor)

    estimates = residuals.rolling_beta(returns, factor, window=200, minimum=200)
    without_shift = returns.rolling(200, min_periods=200).cov(factor) / \
        factor.rolling(200, min_periods=200).var(ddof=1)

    # Сдвиг ровно на один бар.
    pd.testing.assert_series_equal(estimates["beta"].dropna().reset_index(drop=True),
                                   without_shift.shift(1).dropna().reset_index(drop=True),
                                   check_names=False)


def test_residual_removes_the_common_move():
    # Актив, который целиком объясняется фактором, не должен давать остатка -
    # иначе в день, когда падает всё, каждый актив выглядел бы аномалией.
    rng = np.random.default_rng(2)
    factor_values = rng.normal(0, 0.01, 900)
    frame = frame_with(list(2.0 * factor_values))
    factor = pd.Series(factor_values, index=frame["hour_utc"])

    out = residuals.residuals(asset(), frame, factor)
    settled = out["e_resid"].dropna().iloc[300:]

    assert settled.abs().max() < 1e-6


def test_residual_keeps_an_idiosyncratic_move():
    rng = np.random.default_rng(3)
    factor_values = rng.normal(0, 0.01, 900)
    own = np.zeros(900)
    own[800] = 0.10                      # собственное движение актива
    frame = frame_with(list(2.0 * factor_values + own))
    factor = pd.Series(factor_values, index=frame["hour_utc"])

    out = residuals.residuals(asset(), frame, factor)
    assert out["e_resid"].iloc[800] == pytest.approx(0.10, abs=1e-3)


def test_residual_has_its_own_long_run_sigma():
    rng = np.random.default_rng(4)
    factor_values = rng.normal(0, 0.01, 1000)
    frame = frame_with(list(rng.normal(0, 0.02, 1000)))
    factor = pd.Series(factor_values, index=frame["hour_utc"])

    out = residuals.residuals(asset(), frame, factor)
    # До 720 баров сигма остатка не определена, как и у цены.
    assert out["sigma_lt_resid"].iloc[:719].isna().all()
    assert out["sigma_lt_resid"].dropna().size > 0


def test_residual_winsorisation_clips_only_the_state_input():
    rng = np.random.default_rng(5)
    factor_values = rng.normal(0, 0.01, 900)
    own = np.zeros(900)
    own[850] = 0.5
    frame = frame_with(list(factor_values + own))
    factor = pd.Series(factor_values, index=frame["hour_utc"])

    out = residuals.residuals(asset(), frame, factor)
    spike = out.iloc[850]
    assert abs(spike["e_resid"]) > abs(spike["e_resid_w"])


def test_q95_resid_is_computed_but_unused():
    # П.3.6: Q95_resid рассчитывается и хранится ИСКЛЮЧИТЕЛЬНО для диагностики,
    # ни в одном условии документа он не участвует - в п.8.2 работает Q99.
    rng = np.random.default_rng(6)
    factor_values = rng.normal(0, 0.01, 1200)
    frame = frame_with(list(rng.normal(0, 0.02, 1200)))
    factor = pd.Series(factor_values, index=frame["hour_utc"])

    out = residuals.score_residuals(residuals.residuals(asset(), frame, factor), 100)
    assert "q95_resid" in out
    assert "q99_resid" in out


def test_empty_input_keeps_columns():
    empty = pd.DataFrame({"hour_utc": [], "close": [], "r": []})
    out = residuals.residuals(asset(), empty, pd.Series(dtype="float64"))
    assert out.empty
    assert {"e_resid", "beta", "sigma_lt_resid"} <= set(out.columns)
