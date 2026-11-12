"""Who may trade what, and how much: the trading rules, as kept in SQL Server (electronx.users,
user_roles, accounts, account_access, entitlements, limits). No database code here: trading_db.rules
loads them.

Every entitlement and limit is keyed by trader, market and product, where "*" means any:

* Entitlements: a trader may trade a contract if any row matches it, e.g. ("alice", "ERCOT", "*")
  lets alice trade every ERCOT contract. Everyone may still see every product.
* Limits: for each limit, the most specific row that sets it wins, in this order:
  (trader, market, product), (trader, market, *), (trader, *, product), (trader, *, *), then the
  same four for "*" (the desk default). The per-trader limits (max open orders, max loss) come only
  from rows for all markets and products.

The rules fail closed: someone not set up as a trader, a contract no entitlement covers, or a limit
no row sets all mean the order is rejected, never let through.

People are known the way ElectronX knows them: by their ElectronX login, which is their company email
in lower case. It is the id everywhere here (the rules, the orders, the sign-in) and the SenderSubID(50)
on every order we send for them; the exchange's reports name them by it too (TargetSubID 57), on
orders from the ElectronX screen as well. So nothing maps one id to another.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields

from .instruments import Symbol

ANY = "*"
TRADER_WIDE = ("max_open_orders", "max_loss")  # per trader across everything, not per contract
ROLES = ("trader", "risk", "management")


@dataclass(frozen=True)
class Trader:
    """Someone with a login (electronx.users), not only traders: `roles` says what they do here, and only
    a "trader" may place orders."""

    trader_id: str  # their ElectronX login = company email, lower case: jane@greenwichcm.com
    active: bool = True
    roles: frozenset[str] = frozenset({"trader"})
    name: str = ""
    accounts: tuple[str, ...] = ()  # the ElectronX accounts they may trade on (Account, tag 1)


@dataclass(frozen=True)
class Account:
    """An ElectronX trading account (electronx.accounts)."""

    account_id: str  # TA-HURON-00001
    capacity: str = "2"  # CustOrderCapacity(582) sent with every order on it
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
    accounts: Mapping[str, Account] = field(default_factory=dict)

    @classmethod
    def everyone(cls, traders: Iterable[str], account: str = "TA-TEST", **limits: float) -> TradingRules:
        """Every listed trader may trade everything on `account`, within one set of desk limits (for tests)."""
        return cls(
            traders={t: Trader(t, accounts=(account,)) for t in traders},
            entitlements=frozenset({(ANY, ANY, ANY)}),
            limits={(ANY, ANY, ANY): Limits(**limits)},
            accounts={account: Account(account)},
        )

    def check_trader(self, trader: str, symbol: str) -> str | None:
        """Why `trader` may not trade `symbol`, or None if they may."""
        t = self.traders.get(trader)
        if t is None:
            return f"{trader} is not set up to trade"
        if not t.active:
            return f"{trader} is not active"
        if "trader" not in t.roles:
            return f"{trader} is not a trader (roles: {', '.join(sorted(t.roles)) or 'none'})"
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

    def trader_for(self, exchange_user: str | None) -> str | None:
        """Who an ElectronX login is here (TargetSubID 57 on the exchange's reports), in any case: their id,
        which is that login in lower case, or None if nobody here has it."""
        user = (exchange_user or "").strip().lower()
        return user if user and user in self.traders else None

    def account_for(self, trader: str, account: str | None = None) -> tuple[Account | None, str]:
        """The account an order of `trader`'s goes on: `account` if they may use it, else their only one.
        (None, why not) if there is none to use."""
        t = self.traders.get(trader)
        mine = [a for a in (t.accounts if t else ()) if a in self.accounts and self.accounts[a].active]
        if account:
            if account not in mine:
                return None, f"{trader} may not trade on account {account}"
            return self.accounts[account], ""
        if not mine:
            return None, f"no ElectronX account is set up for {trader} (electronx.account_access)"
        if len(mine) > 1:
            return None, f"{trader} may trade on {', '.join(mine)}: say which account"
        return self.accounts[mine[0]], ""

    def summary(self) -> str:
        active = sum(1 for t in self.traders.values() if t.active and "trader" in t.roles)
        return (
            f"{len(self.traders)} users ({active} active traders), {len(self.accounts)} accounts, "
            f"{len(self.entitlements)} entitlements, {len(self.limits)} limit rows"
        )
