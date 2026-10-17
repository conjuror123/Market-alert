from price_monitor.health import record_failure, record_success, should_alert_down


def test_record_failure_increments_streak():
    state = {}
    assert record_failure(state) == 1
    assert record_failure(state) == 2
    assert record_failure(state) == 3


def test_record_success_resets_and_returns_previous_streak():
    state = {}
    record_failure(state)
    record_failure(state)
    previous = record_success(state)
    assert previous == 2
    assert record_failure(state) == 1  # streak restarts from zero


def test_record_success_on_healthy_state_returns_zero():
    state = {}
    assert record_success(state) == 0


def test_should_alert_down_fires_exactly_at_threshold():
    assert should_alert_down(streak=2, alert_after=3, reminder_every=24) is False
    assert should_alert_down(streak=3, alert_after=3, reminder_every=24) is True
    assert should_alert_down(streak=4, alert_after=3, reminder_every=24) is False


def test_should_alert_down_reminds_periodically():
    assert should_alert_down(streak=27, alert_after=3, reminder_every=24) is True
    assert should_alert_down(streak=26, alert_after=3, reminder_every=24) is False
    assert should_alert_down(streak=51, alert_after=3, reminder_every=24) is True


def test_should_alert_down_no_reminder_when_disabled():
    assert should_alert_down(streak=3, alert_after=3, reminder_every=0) is True
    assert should_alert_down(streak=10, alert_after=3, reminder_every=0) is False


def test_should_alert_down_disabled_when_alert_after_zero():
    assert should_alert_down(streak=100, alert_after=0, reminder_every=24) is False
