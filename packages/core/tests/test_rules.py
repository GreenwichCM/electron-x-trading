from trading_core.rules import ANY, Limits, Trader, TradingRules, market_and_product

ERCOT = "BFUT500-ERCOT-HB_NORTH-261231-15"
ERCOT_100 = "BFUT100-ERCOT-HB_WEST-261231-15"
PJM = "BFUT100-PJM-WESTERN_HUB-261231-15"

RULES = TradingRules(
    traders={"alice": Trader("alice"), "dave": Trader("dave", "dave@example.com"), "eve": Trader("eve", active=False)},
    entitlements=frozenset({("alice", "ERCOT", ANY), ("dave", "PJM", "BFUT100"), ("eve", ANY, ANY)}),
    limits={
        (ANY, ANY, ANY): Limits(10, 5000.0, 20, 50, 5000.0),
        ("alice", "ERCOT", "BFUT500"): Limits(max_position=20),
        ("alice", ANY, ANY): Limits(max_loss=1000.0, max_open_orders=5),
        ("dave", ANY, "BFUT100"): Limits(max_order_qty=3),
        ("dave", "PJM", ANY): Limits(max_open_orders=99),  # ignored: open orders are per trader, not per market
    },
)


def test_symbols_split_into_market_and_product():
    assert market_and_product(ERCOT) == ("ERCOT", "BFUT500")
    assert market_and_product("X") == ("?", "?")


def test_entitlements():
    assert RULES.check_trader("alice", ERCOT) is None and RULES.check_trader("alice", ERCOT_100) is None
    assert RULES.check_trader("alice", PJM) == "alice is not entitled to trade PJM BFUT100"
    assert RULES.check_trader("dave", PJM) is None
    assert "not entitled" in RULES.check_trader("dave", "BFUT500-PJM-WESTERN_HUB-261231-15")
    assert RULES.check_trader("eve", ERCOT) == "eve is not active"
    assert RULES.check_trader("mallory", ERCOT) == "mallory is not set up to trade"


def test_the_most_specific_limit_wins():
    assert RULES.limits_for("alice", ERCOT) == Limits(10, 5000.0, 5, 20, 1000.0)
    assert RULES.limits_for("alice", ERCOT_100).max_position == 50  # the override is for BFUT500 only
    assert RULES.limits_for("dave", PJM) == Limits(3, 5000.0, 20, 50, 5000.0)
    assert RULES.limits_for("alice").max_loss == 1000.0


def test_a_limit_nobody_set_is_missing():
    rules = TradingRules(traders={}, entitlements=frozenset(), limits={(ANY, ANY, ANY): Limits(max_order_qty=1)})
    assert rules.limits_for("alice", ERCOT).missing("max_order_qty", "max_position") == ["max_position"]


def test_everyone_and_exchange_trader_ids():
    rules = TradingRules.everyone(["alice"], max_order_qty=10)
    assert rules.check_trader("alice", PJM) is None and rules.limits_for("alice", PJM).max_order_qty == 10
    assert RULES.exchange_trader("dave") == "dave@example.com" and RULES.exchange_trader("alice") is None


def test_trader_for_an_electronx_id():
    rules = TradingRules(
        traders={
            "alice": Trader("alice", "firms/h/users/alice"),
            "dave": Trader("dave", "shared"),
            "erin": Trader("erin", "shared"),
            "bob": Trader("bob"),
        },
        entitlements=frozenset(),
        limits={},
    )
    assert rules.trader_for("firms/h/users/alice") == "alice"
    assert rules.trader_for("shared") is None  # two traders have it: it cannot say who traded
    assert rules.trader_for("nobody") is None and rules.trader_for("") is None and rules.trader_for(None) is None


def test_an_electronx_id_matches_whatever_its_case():
    rules = TradingRules(traders={"jane": Trader("jane", "Jane@x.com")}, entitlements=frozenset(), limits={})
    assert rules.trader_for("jane@X.COM") == "jane" and rules.trader_for("Jane@x.com") == "jane"
