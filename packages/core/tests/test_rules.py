from trading_core.rules import ANY, Account, Limits, Trader, TradingRules, market_and_product

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


def test_everyone_may_trade_on_one_account():
    rules = TradingRules.everyone(["alice"], max_order_qty=10)
    assert rules.check_trader("alice", PJM) is None and rules.limits_for("alice", PJM).max_order_qty == 10
    assert rules.account_for("alice") == (Account("TA-TEST"), "")


def test_a_user_is_their_electronx_login_in_any_case():
    rules = TradingRules(traders={"jane@x.com": Trader("jane@x.com")}, entitlements=frozenset(), limits={})
    assert rules.trader_for("jane@x.com") == "jane@x.com" and rules.trader_for("Jane@X.com") == "jane@x.com"
    assert rules.trader_for("nobody@x.com") is None and rules.trader_for("") is None and rules.trader_for(None) is None


def test_only_traders_trade_and_only_on_their_accounts():
    rules = TradingRules(
        traders={
            "jane@x.com": Trader("jane@x.com", accounts=("A1", "A2")),
            "rick@x.com": Trader("rick@x.com", roles=frozenset({"risk"})),
            "tom@x.com": Trader("tom@x.com"),
        },
        entitlements=frozenset({(ANY, ANY, ANY)}),
        limits={},
        accounts={"A1": Account("A1", capacity="1"), "A2": Account("A2"), "A3": Account("A3")},
    )
    assert rules.check_trader("rick@x.com", PJM) == "rick@x.com is not a trader (roles: risk)"
    assert rules.account_for("jane@x.com", "A1") == (Account("A1", capacity="1"), "")
    assert rules.account_for("jane@x.com", "A3")[1] == "jane@x.com may not trade on account A3"
    assert "say which account" in rules.account_for("jane@x.com")[1]
    assert "no ElectronX account" in rules.account_for("tom@x.com")[1]
