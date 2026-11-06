"""Who may trade what, and how much: the trading rules, as kept in SQL Server (electronx.traders,
electronx.entitlements, electronx.limits). No database code here: trading_db.rules loads them.

Every entitlement and limit is keyed by trader, market and product, where "*" means any:

* Entitlements: a trader may trade a contract if any row matches it, e.g. ("alice", "ERCOT", "*")
  lets alice trade every ERCOT contract. Everyone may still see every product.
* Limits: for each limit, the most specific row that sets it wins, in this order:
  (trader, market, product), (trader, market, *), (trader, *, product), (trader, *, *), then the
  same four for "*" (the desk default). The per-trader limits (max open orders, max loss) come only
  from rows for all markets and products.

The rules fail closed: someone not set up as a trader, a contract no entitlement covers, or a limit
no row sets all mean the order is rejected, never let through.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields

from .instruments import Symbol

ANY = "*"
TRADER_WIDE = ("max_open_orders", "max_loss")  # per trader across everything, not per contract


@dataclass(frozen=True)
class Trader:
    trader_id: str  # our user id, e.g. alice
    exchange_trader_id: str | None = None  # ElectronX trader id for SenderSubID(50); None: the default
    active: bool = True


@dataclass(frozen=True)
class Limits:
    max_order_qty: int | None = None  # lots per order
    max_order_notional: float | None = None  # quantity x price per order
    max_open_orders: int | None = None  # per trader, all contracts
    max_position: int | None = None  # lots per contract, counting resting orders (worst case)
    max_loss: float | None = None  # USD per trader; riskmonitor blocks the trader on reaching it

    def missing(self, *names: str) -> list[str]:
        return [n for n in names if getattr(self, n) is None]


LIMIT_NAMES = tuple(f.name for f in fields(Limits))


def market_and_product(symbol: str) -> tuple[str, str]:
    """("ERCOT", "BFUT500") for BFUT500-ERCOT-HB_NORTH-261231-15; ("?", "?") for anything unparseable,
    which then only rules for every market and product ("*") can cover."""
    try:
        s = Symbol.parse(symbol)
    except ValueError:
        return "?", "?"
    return s.market.value, s.product_code


@dataclass(frozen=True)
class TradingRules:
    traders: Mapping[str, Trader]
    entitlements: frozenset[tuple[str, str, str]]  # (trader, market, product)
    limits: Mapping[tuple[str, str, str], Limits]  # (trader, market, product) -> limits set on that row

    @classmethod
    def everyone(cls, traders: Iterable[str], **limits: float) -> TradingRules:
        """Every listed trader may trade everything, within one set of desk limits (for tests)."""
        return cls(
            traders={t: Trader(t) for t in traders},
            entitlements=frozenset({(ANY, ANY, ANY)}),
            limits={(ANY, ANY, ANY): Limits(**limits)},
        )

    def check_trader(self, trader: str, symbol: str) -> str | None:
        """Why `trader` may not trade `symbol`, or None if they may."""
        t = self.traders.get(trader)
        if t is None:
            return f"{trader} is not set up to trade"
        if not t.active:
            return f"{trader} is not active"
        market, product = market_and_product(symbol)
        for who in (trader, ANY):
            for m in (market, ANY):
                for p in (product, ANY):
                    if (who, m, p) in self.entitlements:
                        return None
        return f"{trader} is not entitled to trade {market} {product}"

    def limits_for(self, trader: str, symbol: str | None = None) -> Limits:
        """The limits that apply to `trader` on `symbol` (or overall, without a symbol)."""
        market, product = market_and_product(symbol) if symbol else (ANY, ANY)
        scopes = list(dict.fromkeys([(market, product), (market, ANY), (ANY, product), (ANY, ANY)]))
        values = {}
        for name in LIMIT_NAMES:
            keys = [
                (who, *scope) for who in (trader, ANY) for scope in ([(ANY, ANY)] if name in TRADER_WIDE else scopes)
            ]
            values[name] = next(
                (
                    getattr(row, name)
                    for key in keys
                    if (row := self.limits.get(key)) and getattr(row, name) is not None
                ),
                None,
            )
        return Limits(**values)

    def exchange_trader(self, trader: str) -> str | None:
        t = self.traders.get(trader)
        return t.exchange_trader_id if t else None

    def summary(self) -> str:
        active = sum(1 for t in self.traders.values() if t.active)
        return f"{active} active traders, {len(self.entitlements)} entitlements, {len(self.limits)} limit rows"
