"""Who the caller is (authentication) and what they may do (permissions).

Only the Authenticator changes when the company login (Entra ID or AD) arrives: it turns a token
into a User. Everything after that point, the roles, permissions and checks, stays the same.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import jwt

#: Role -> permissions. Moves to SQL Server (role_permissions) later; the names stay the same.
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


DEV_USERS: dict[str, User] = {
    "alice": User("alice", "Alice (trader)", ("trader",)),
    "bob": User("bob", "Bob (risk)", ("risk",)),
    "carol": User("carol", "Carol (management)", ("management",)),
    "dave": User("dave", "Dave (trader)", ("trader",)),
    "erin": User("erin", "Erin (risk)", ("risk",)),  # a second risk user, to approve bob's changes
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
