"""Who the caller is (authentication) and what they may do (permissions).

A user's id is their ElectronX login, their company email in lower case; their name and roles come from
electronx.users and user_roles in SQL Server. Only the Authenticator changes when Google sign-in arrives:
it turns a token into a User. Everything after that point, the roles, permissions and checks, stays the
same.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from trading_core import TradingRules

#: Role -> permissions. Who has which role is in SQL Server (electronx.user_roles).
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "trader": frozenset({"md.view", "orders.view.own", "orders.trade", "risk.view.own", "risk.view.desk_aggregate"}),
    "risk": frozenset(
        {
            "md.view",
            "orders.view.own",
            "orders.view.all",
            "risk.view.own",
            "risk.view.all",
            "risk.view.desk_aggregate",
            "killswitch.trip",
            "limits.edit",
            "limits.approve",
        }
    ),
    "management": frozenset(
        {"md.view", "orders.view.own", "orders.view.all", "risk.view.own", "risk.view.all", "risk.view.desk_aggregate"}
    ),
}


@dataclass(frozen=True)
class User:
    id: str
    name: str
    roles: tuple[str, ...]

    @property
    def permissions(self) -> frozenset[str]:
        return frozenset().union(*(ROLE_PERMISSIONS.get(r, frozenset()) for r in self.roles))

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "roles": list(self.roles), "permissions": sorted(self.permissions)}


class Authenticator(Protocol):
    def authenticate(self, token: str) -> User | None:
        """The user a token belongs to, or None if it is missing, forged or expired."""
        ...


#: The test users when there is no database (the api's own tests); otherwise electronx.users has them.
DEV_USERS: dict[str, User] = {
    u.id: u
    for u in (
        User("alice@example.test", "Alice (trader)", ("trader",)),
        User("bob@example.test", "Bob (risk)", ("risk",)),
        User("carol@example.test", "Carol (management)", ("management",)),
        User("dave@example.test", "Dave (trader)", ("trader",)),
        User("erin@example.test", "Erin (risk)", ("risk",)),  # a second risk user, to approve bob's changes
    )
}


def users_from_rules(rules: TradingRules) -> dict[str, User]:
    """Everyone active in electronx.users, with their roles."""
    return {
        t.trader_id: User(t.trader_id, t.name or t.trader_id, tuple(sorted(t.roles)))
        for t in rules.traders.values()
        if t.active
    }


class DevAuthenticator:
    """Development login: pick a test user, get a token signed with a local secret.

    Never for production: anyone who can reach the login endpoint can be anyone. The api refuses to
    turn it on unless it listens on localhost only.
    """

    ISSUER = "trading-dev"

    def __init__(self, secret: str, users: dict[str, User] = DEV_USERS, ttl_s: int = 8 * 3600):
        self.secret = secret
        self.users = users
        self.ttl_s = ttl_s

    def set_users(self, users: dict[str, User]) -> None:
        """Who may sign in from now on; a token of someone no longer here stops working."""
        self.users = users

    def issue(self, user_id: str) -> str:
        now = int(time.time())
        claims = {"sub": user_id, "iss": self.ISSUER, "iat": now, "exp": now + self.ttl_s}
        return jwt.encode(claims, self.secret, algorithm="HS256")

    def authenticate(self, token: str) -> User | None:
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"], issuer=self.ISSUER)
        except jwt.InvalidTokenError:
            return None
        return self.users.get(claims.get("sub", ""))
