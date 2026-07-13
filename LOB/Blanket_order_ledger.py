"""
Blanket order ledger: tracks total shares committed, shares sold, and
shares remaining per stock/blanket agreement, matching Stage 7's "Blanket
Order Update" and Stage 8's "Inventory Update" from the pipeline doc.

Deliberately NOT stored as custom Odoo fields - "shares committed/sold/
remaining" isn't a native Odoo concept, and adding custom fields (ir.model.fields)
is an extra layer of Odoo configuration that varies by install and is easy
to get wrong via API. A simple local JSON ledger is more robust and easier
to inspect/debug; if you later want this visible IN Odoo itself, the
values here can be written into a real custom field once you know exactly
which field setup your install supports.
"""

import json
import os
from dataclasses import dataclass, asdict
from typing import Optional


LEDGER_PATH = "blanket_orders.json"


@dataclass
class BlanketOrderState:
    stock_id: str
    total_committed: float
    shares_sold: float

    @property
    def shares_remaining(self) -> float:
        return self.total_committed - self.shares_sold

    @property
    def is_fulfilled(self) -> bool:
        return self.shares_remaining <= 0


class BlanketOrderLedger:
    def __init__(self, path: str = LEDGER_PATH):
        self.path = path
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path) as f:
                return json.load(f)
        return {}

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    def initialize(self, stock_id: str, total_committed: float, overwrite: bool = False):
        """Sets up a blanket order for a stock. Call once when the
        agreement is created - overwrite=False refuses to clobber an
        existing in-progress ledger by accident."""
        if stock_id in self._data and not overwrite:
            raise ValueError(
                f"Blanket order for {stock_id!r} already exists "
                f"(committed={self._data[stock_id]['total_committed']}, "
                f"sold={self._data[stock_id]['shares_sold']}). "
                "Pass overwrite=True if you really want to reset it."
            )
        self._data[stock_id] = {"total_committed": total_committed, "shares_sold": 0.0}
        self._save()

    def record_sale(self, stock_id: str, quantity_sold: float) -> BlanketOrderState:
        """
        Call this once per completed trade (Stage 7/8: after a sale order
        is confirmed and settled). Updates shares_sold and returns the new
        state, including whether the blanket order is now fully fulfilled.
        """
        if stock_id not in self._data:
            raise ValueError(
                f"No blanket order initialized for {stock_id!r}. "
                "Call initialize() first with the total committed shares."
            )

        entry = self._data[stock_id]
        entry["shares_sold"] += quantity_sold
        self._save()

        state = BlanketOrderState(
            stock_id=stock_id,
            total_committed=entry["total_committed"],
            shares_sold=entry["shares_sold"],
        )
        return state

    def get_state(self, stock_id: str) -> Optional[BlanketOrderState]:
        entry = self._data.get(stock_id)
        if entry is None:
            return None
        return BlanketOrderState(
            stock_id=stock_id,
            total_committed=entry["total_committed"],
            shares_sold=entry["shares_sold"],
        )


if __name__ == "__main__":
    import tempfile

    # Self-contained test using a temp file, doesn't touch your real ledger
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test_ledger.json")
        ledger = BlanketOrderLedger(path)

        ledger.initialize("ACME_CORP_STOCK", total_committed=1000)
        print("Initialized:", ledger.get_state("ACME_CORP_STOCK"))

        state = ledger.record_sale("ACME_CORP_STOCK", 311)
        print(f"After trade 1: sold={state.shares_sold}, remaining={state.shares_remaining}, "
              f"fulfilled={state.is_fulfilled}")

        state = ledger.record_sale("ACME_CORP_STOCK", 486)
        print(f"After trade 2: sold={state.shares_sold}, remaining={state.shares_remaining}, "
              f"fulfilled={state.is_fulfilled}")

        state = ledger.record_sale("ACME_CORP_STOCK", 203)
        print(f"After trade 3: sold={state.shares_sold}, remaining={state.shares_remaining}, "
              f"fulfilled={state.is_fulfilled}")

        assert state.shares_remaining == 0
        assert state.is_fulfilled
        print("\nMatches your 3 real winners exactly (311+486+203=1000) - fully fulfilled.")