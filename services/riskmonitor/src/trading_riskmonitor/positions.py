"""A position in one contract, with average cost and realized P&L.

Average cost: buying adds to the position at the weighted average price; selling out of a long (or
buying back a short) realizes (exit price - average price) x lots and leaves the average unchanged.
Crossing through zero closes the old position and opens a new one at the fill price.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_core import LOT_SIZE_MWH


@dataclass
class Position:
    qty: int = 0  # lots; positive long, negative short
    avg_px: float = 0.0  # USD/MWh
    realized: float = 0.0  # USD

    def apply(self, side: str, qty: int, price: float) -> None:
        signed = qty if side == "BUY" else -qty
        if self.qty == 0 or (self.qty > 0) == (signed > 0):  # opening or adding
            new_qty = self.qty + signed
            self.avg_px = (self.avg_px * abs(self.qty) + price * qty) / abs(new_qty)
            self.qty = new_qty
            return
        closing = min(qty, abs(self.qty))
        direction = 1 if self.qty > 0 else -1
        self.realized += closing * (price - self.avg_px) * direction * LOT_SIZE_MWH
        self.qty += signed
        if self.qty == 0:
            self.avg_px = 0.0
        elif qty > closing:  # went through zero: the rest is a new position at this price
            self.avg_px = price

    def unrealized(self, mark: float | None) -> float:
        if mark is None or self.qty == 0:
            return 0.0
        return self.qty * (mark - self.avg_px) * LOT_SIZE_MWH
