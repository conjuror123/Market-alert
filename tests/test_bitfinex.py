from price_monitor import bitfinex


def test_close_comes_before_high_and_untraded_hours_are_dropped():
    rows = [[7200000, 1.0, 1.1, 1.3, 0.9, 5.0], [3600000, 1.0, 1.0, 1.0, 1.0, 0.0]]
    out = bitfinex.parse(rows)
    assert len(out) == 1
    c = out[0]
    assert (c.open_time, c.open, c.close, c.high, c.low) == (7200, 1.0, 1.1, 1.3, 0.9)
