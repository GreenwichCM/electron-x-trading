from trading_core import KillSwitchState


def test_round_trip():
    s = KillSwitchState(active=True, reason="position break", by="dropcopy", time="2026-11-05T10:00:00Z")
    assert KillSwitchState.from_json(s.to_json()) == s


def test_missing_key_means_off():
    assert KillSwitchState.from_json(None).active is False
    assert KillSwitchState.from_json(b"").active is False
