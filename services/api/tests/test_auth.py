import pytest
from trading_api import DEV_USERS, ConfigError, DevAuthenticator, Settings

SECRET = "x" * 32


def test_token_round_trip():
    auth = DevAuthenticator(SECRET)
    assert auth.authenticate(auth.issue("alice@example.test")) == DEV_USERS["alice@example.test"]


def test_forged_expired_and_unknown_tokens_are_rejected():
    auth = DevAuthenticator(SECRET)
    other = DevAuthenticator("y" * 32)
    assert auth.authenticate(other.issue("alice@example.test")) is None  # signed with a different secret
    assert auth.authenticate(DevAuthenticator(SECRET, ttl_s=-1).issue("alice@example.test")) is None  # expired
    assert auth.authenticate(auth.issue("mallory")) is None  # not a known user
    assert auth.authenticate("not-a-token") is None


def test_role_permissions():
    alice, bob, carol = DEV_USERS["alice@example.test"], DEV_USERS["bob@example.test"], DEV_USERS["carol@example.test"]
    assert alice.can("orders.trade") and not alice.can("orders.view.all") and not alice.can("killswitch.trip")
    assert bob.can("killswitch.trip") and bob.can("orders.view.all") and not bob.can("orders.trade")
    assert carol.can("risk.view.all") and not carol.can("killswitch.trip") and not carol.can("orders.trade")
    assert all(u.can("md.view") for u in (alice, bob, carol))


def test_dev_login_only_on_localhost():
    with pytest.raises(ConfigError):
        Settings(host="0.0.0.0", dev_login=True, dev_secret=SECRET)


def test_dev_login_needs_a_real_secret():
    with pytest.raises(ConfigError):
        Settings(dev_login=True, dev_secret="short")
