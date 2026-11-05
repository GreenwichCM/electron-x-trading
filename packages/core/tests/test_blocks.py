from trading_core import TraderBlock, decode_blocks


def test_decode_blocks_from_redis():
    block = TraderBlock(reason="daily loss", by="riskmonitor", time="t")
    assert decode_blocks({b"alice": block.to_json().encode()}) == {"alice": block}
    assert decode_blocks({}) == {}
