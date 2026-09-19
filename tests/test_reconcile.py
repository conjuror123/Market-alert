"""Deleting what an older detector sent and a newer one no longer stands behind."""
import pytest

from price_monitor import follow_up, reconcile, tremor_delivery
from price_monitor.config import Config

HOUR = 1789642800


def _state(**tracked):
    return {tremor_delivery.STATE_KEY: {
        follow_up.TRACKED: {k: {"hour_utc": HOUR, "message_id": v,
                                "text_hash": "h", "written": []}
                            for k, v in tracked.items()},
        tremor_delivery._SENT: {k: {"hour": HOUR, "id": v, "hash": "h"}
                                for k, v in tracked.items()},
    }}


def _event(event_id, channel="push"):
    return {"event_id": event_id, "channel": channel, "hour_utc": HOUR,
            "asset_id": "twelvedata:GLD", "tier": "major"}


def test_a_push_whose_event_vanished_is_planned_for_deletion():
    # The partial-bar fix took whole hours out of the table: a move scored from
    # five minutes of trading qualified, and the same hour scored whole does
    # not. The alert is then about nothing at all.
    actions = reconcile.plan([_event("other")], _state(gone=22))
    assert [a["message_id"] for a in actions] == [22]
    assert "no longer an event" in actions[0]["reason"]


def test_a_push_that_is_now_a_digest_row_is_planned_for_deletion():
    # Not gone, demoted: it should have been a quiet row, not an interruption.
    actions = reconcile.plan([_event("demoted", channel="digest")],
                             _state(demoted=19))
    assert [a["message_id"] for a in actions] == [19]


def test_the_reason_says_whether_a_note_still_carries_the_move():
    # "You have this twice" and "this is leaving the channel" are different
    # things to be told before something is deleted irreversibly.
    event = _event("demoted", channel="digest")
    state = _state(demoted=19)

    alone = reconcile.plan([event], state)[0]["reason"]
    assert "no note carries it" in alone

    state[tremor_delivery.STATE_KEY][tremor_delivery.DIGEST_STATE] = {
        "1": {"ids": [1], "hashes": ["h"], "from": 1, "to": 2, "events": ["demoted"]}}
    carried = reconcile.plan([event], state)[0]["reason"]
    assert "already carries it" in carried


def test_a_push_that_is_still_a_push_is_left_alone():
    assert reconcile.plan([_event("live")], _state(live=14)) == []


def test_state_is_only_forgotten_for_a_message_telegram_confirms_gone(monkeypatch):
    # A refused delete - the 48-hour rule in a private chat - must leave the
    # record alone. Forgetting it would declare the message handled while it is
    # still on the phone, and the next plan would not even mention it.
    refused = []
    monkeypatch.setattr(reconcile, "delete_telegram_message",
                        lambda t, c, m: (refused.append(m), False)[1])
    monkeypatch.setattr(reconcile, "save_state", lambda *a: None)
    state = _state(stuck=19)
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c", state_path="/dev/null")

    gone = reconcile.apply(cfg, state, reconcile.plan([], state))

    assert gone == 0 and refused == [19]
    store = state[tremor_delivery.STATE_KEY]
    assert "stuck" in store[follow_up.TRACKED], "a refused delete must not be forgotten"


def test_a_confirmed_delete_is_forgotten_from_both_maps(monkeypatch):
    monkeypatch.setattr(reconcile, "delete_telegram_message", lambda t, c, m: True)
    monkeypatch.setattr(reconcile, "save_state", lambda *a: None)
    state = _state(gone=22)
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c", state_path="/dev/null")

    assert reconcile.apply(cfg, state, reconcile.plan([_event("other")], state)) == 1
    store = state[tremor_delivery.STATE_KEY]
    assert store[follow_up.TRACKED] == {} and store[tremor_delivery._SENT] == {}


def test_an_empty_table_deletes_nothing(monkeypatch, tmp_path):
    # THE GUARD THAT MATTERS. An empty events table is "the pipeline did not
    # run", not "every event vanished" - delivery works to the same rule. Read
    # the other way it would clear the phone.
    deleted = []
    monkeypatch.setattr(reconcile, "delete_telegram_message",
                        lambda t, c, m: deleted.append(m) or True)
    monkeypatch.setattr(reconcile, "load_events", lambda cfg: [], raising=False)
    monkeypatch.setattr(reconcile.tremor_delivery, "load_events", lambda cfg: [])
    monkeypatch.setattr(reconcile, "load_config",
                        lambda: Config(telegram_bot_token="t", telegram_chat_id="c",
                                       state_path=str(tmp_path / "state.json")))
    assert reconcile.main(["--apply"]) == 2
    assert deleted == []


def test_the_plan_alone_changes_nothing(monkeypatch, tmp_path):
    deleted = []
    monkeypatch.setattr(reconcile, "delete_telegram_message",
                        lambda t, c, m: deleted.append(m) or True)
    monkeypatch.setattr(reconcile.tremor_delivery, "load_events",
                        lambda cfg: [_event("other")])
    path = tmp_path / "state.json"
    import json
    path.write_text(json.dumps(_state(gone=22)))
    monkeypatch.setattr(reconcile, "load_config",
                        lambda: Config(telegram_bot_token="t", telegram_chat_id="c",
                                       state_path=str(path)))
    assert reconcile.main([]) == 0
    assert deleted == [], "a dry run must not touch Telegram"


# --- shedding the parts a frozen note no longer needs --------------------------

def _note(slot, ids, events=None, to=None):
    record = {"ids": list(ids), "hashes": ["h"] * len(ids),
              "from": slot, "to": to if to is not None else slot + 3 * 24 * 3600}
    if events is not None:
        record["events"] = events
    return {tremor_delivery.STATE_KEY: {tremor_delivery.DIGEST_STATE: {str(slot): record}},
            }


def test_freeze_records_what_a_note_published():
    slot = 1789344300
    state = _note(slot, [13, 27, 28])
    reconcile.freeze(state, slot, ["a", "b"])
    record = state[tremor_delivery.STATE_KEY][tremor_delivery.DIGEST_STATE][str(slot)]
    assert record["events"] == ["a", "b"] and record["rows"] == 2


def test_freeze_refuses_a_note_that_does_not_exist():
    with pytest.raises(KeyError):
        reconcile.freeze(_note(1, [1]), 999, ["a"])


def test_surplus_parts_are_shed_newest_first_and_the_ids_stay_a_prefix(monkeypatch):
    # Newest first because dropping a middle part renumbers the ones after it,
    # and every later edit would then land on the wrong message.
    deleted = []
    monkeypatch.setattr(reconcile, "delete_telegram_message",
                        lambda t, c, m: deleted.append(m) or True)
    monkeypatch.setattr(reconcile, "save_state", lambda *a: None)
    slot = 1789344300
    state = _note(slot, [13, 27, 28], events=["a"])
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c", state_path="/dev/null")
    extra = [{"slot": str(slot), "index": 1, "message_id": 27, "reason": "x"},
             {"slot": str(slot), "index": 2, "message_id": 28, "reason": "x"}]

    assert reconcile.shed(cfg, state, extra) == 2
    assert deleted == [28, 27], "the last part must go first"
    record = state[tremor_delivery.STATE_KEY][tremor_delivery.DIGEST_STATE][str(slot)]
    assert record["ids"] == [13] and record["hashes"] == ["h"]


def test_a_refused_part_delete_stops_the_ones_before_it(monkeypatch):
    # If the newest part cannot be deleted, removing the one before it would
    # leave a gap in the middle of the note.
    monkeypatch.setattr(reconcile, "delete_telegram_message", lambda t, c, m: False)
    monkeypatch.setattr(reconcile, "save_state", lambda *a: None)
    slot = 1789344300
    state = _note(slot, [13, 27, 28], events=["a"])
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c", state_path="/dev/null")
    extra = [{"slot": str(slot), "index": 1, "message_id": 27, "reason": "x"},
             {"slot": str(slot), "index": 2, "message_id": 28, "reason": "x"}]

    assert reconcile.shed(cfg, state, extra) == 0
    record = state[tremor_delivery.STATE_KEY][tremor_delivery.DIGEST_STATE][str(slot)]
    assert record["ids"] == [13, 27, 28], "nothing may be forgotten if nothing went"
