"""The details of an ExecutionReport [8], in the field names used by order events and fills.

orderentry (our orders) and dropcopy (orders from the ElectronX screen, and every fill) both record them,
so they read the report the same way. A field the exchange did not send is None. What an order was sent
with (account, capacity, manual) is kept on the order itself, not taken from each report.
"""

from __future__ import annotations

from typing import Any

from .codec import FixMessage


def _int(msg: FixMessage, tag: int) -> int | None:
    value = msg.get(tag)
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _float(msg: FixMessage, tag: int) -> float | None:
    value = msg.get(tag)
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _flag(msg: FixMessage, tag: int) -> bool | None:
    value = msg.get(tag)
    return None if value in (None, "") else value == "Y"


def report_fields(msg: FixMessage) -> dict[str, Any]:
    """What happened at the exchange: the report's own id and type, the fill if it is one, and why an order
    was rejected or cancelled by the exchange."""
    trade = msg.get(150) in ("F", "H")  # a trade, or the bust (cancel) of one: both carry the trade
    return {
        "exec_id": msg.get(17, ""),  # ExecID
        "exec_type": msg.get(150, ""),  # ExecType: 0 new, 4 cancelled, 5 replaced, 8 rejected, C expired,
        # F trade, H trade bust
        "exec_ref_id": msg.get(19) or None,  # ExecRefID: on a bust, the ExecID of the fill it cancels
        "exchange_time": msg.get(60, ""),  # TransactTime, UTC to the nanosecond
        "last_qty": _int(msg, 32) if trade else None,  # LastQty
        "last_px": _float(msg, 31) if trade else None,  # LastPx
        "leaves_qty": _int(msg, 151),  # LeavesQty
        "reject_code": _int(msg, 103),  # OrdRejReason
        "restatement_reason": _int(msg, 378),  # ExecRestatementReason: 8 cancel on disconnect, 99 self-match
        "trade_id": msg.get(880) or None,  # TrdMatchID: the same for buyer and seller
        "aggressor": _flag(msg, 1057),  # AggressorIndicator: this order took liquidity
        "commission": _float(msg, 12),  # Commission
        "account_type": msg.get(581) or None,  # AccountType: 1 customer, 2 non-customer
    }
