import pytest
from trading_fix import FixParseError, FixParser, decode, encode


def test_encode_has_correct_length_and_checksum():
    raw = encode("FIXT.1.1", [(35, "0"), (49, "A"), (56, "B"), (34, "1")])
    assert raw.startswith(b"8=FIXT.1.1\x019=")
    body_len = int(raw.split(b"\x01")[1][2:])
    body = raw[raw.index(b"\x0135=") + 1 : raw.index(b"10=")]
    assert len(body) == body_len
    assert int(raw[-4:-1]) == sum(raw[: raw.index(b"10=")]) % 256


def test_round_trip_and_repeating_groups():
    raw = encode("FIXT.1.1", [(35, "y"), (146, "2"), (55, "X"), (969, "0.25"), (55, "Y"), (969, "0.5")])
    msg = decode(raw)
    assert msg.msg_type == "y" and msg.get_int(146) == 2
    assert msg.groups(146, 55) == [{55: "X", 969: "0.25"}, {55: "Y", 969: "0.5"}]


def test_password_is_masked_in_repr():
    msg = decode(encode("FIXT.1.1", [(35, "A"), (554, "secret")]))
    assert "secret" not in repr(msg)


def test_parser_handles_split_and_joined_messages():
    a = encode("FIXT.1.1", [(35, "0"), (34, "1")])
    b = encode("FIXT.1.1", [(35, "0"), (34, "2")])
    p = FixParser()
    assert p.feed(a[:7]) == []
    msgs = p.feed(a[7:] + b)
    assert [m.get_int(34) for m in msgs] == [1, 2]


def test_parser_rejects_bad_checksum():
    raw = bytearray(encode("FIXT.1.1", [(35, "0")]))
    raw[-2] = ord("9") if raw[-2] != ord("9") else ord("8")
    with pytest.raises(FixParseError):
        FixParser().feed(bytes(raw))
