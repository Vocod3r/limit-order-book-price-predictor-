"""
Ask-side order book: tracks the seller's resting sell limit orders (asks)
per stock, over time. This is the missing real data source that let
ASK_DEPLETING sit as dead code before - previously the ask price/qty were
frozen constants passed on the command line, so that regime could never
fire correctly no matter what happened.

Kept OUTSIDE Odoo CRM deliberately: per the original pipeline doc, asks
are "existing sell listings" - a different kind of data than incoming
buyer bids (which DO get registered in CRM via bid_ingestion.py). This is
a local, timestamped event log instead - simple JSON, easy to inspect.

Terminal usage (posts a new sell limit order - Event 4 in Step 2.4):
    py ask_book.py <stock_id> <price> <qty>
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Tuple


ASK_BOOK_PATH = "ask_book.json"


class AskBook:
    def __init__(self, path: str = ASK_BOOK_PATH):
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

    def post_new_ask(self, stock_id: str, price: float, quantity: float,
                      timestamp: Optional[datetime] = None) -> dict:
        """
        Event 4 (Step 2.4): a new sell limit order is posted. If the new
        price is lower than the current best ask, it BECOMES the new best
        ask (matches "seller reduces asking price" -> mid-price decreases,
        per the doc). If it's higher or equal, it just adds depth at the
        current level rather than replacing it.
        """
        timestamp = timestamp or datetime.now()
        entry = self._data.setdefault(stock_id, {"price": price, "qty": 0.0, "events": []})

        if entry["qty"] <= 0 or price < entry["price"]:
            # strictly better price - this becomes the new best ask.
            # NOTE: this model only tracks single-level depth (same
            # limitation as the bid side) - old resting liquidity at the
            # previous, worse price is not summed in; it's simply no
            # longer "the best ask" until this new order is exhausted.
            entry["price"] = price
            entry["qty"] = quantity
        elif price == entry["price"]:
            entry["qty"] += quantity  # same level, adds depth
        else:
            pass  # worse price than current best ask - doesn't change best-level state

        entry["events"].append({
            "event_type": "new_sell_limit_order",
            "price": price, "quantity": quantity,
            "resulting_best_ask": entry["price"], "resulting_qty": entry["qty"],
            "timestamp": timestamp.isoformat(),
        })
        self._save()
        return entry

    def deplete(self, stock_id: str, quantity: float,
                timestamp: Optional[datetime] = None) -> Tuple[dict, bool]:
        """
        Event 1 (Step 2.4): the ask queue loses shares (e.g. a trade
        executed against it). Returns (entry, was_fully_depleted).
        """
        timestamp = timestamp or datetime.now()
        entry = self._data.get(stock_id)
        if entry is None:
            raise ValueError(f"No ask book entry for {stock_id!r} - post_new_ask() first.")

        entry["qty"] = max(0.0, entry["qty"] - quantity)
        depleted = entry["qty"] <= 0
        entry["events"].append({
            "event_type": "ask_depleted" if depleted else "ask_reduced",
            "quantity": quantity, "remaining_qty": entry["qty"],
            "timestamp": timestamp.isoformat(),
        })
        self._save()
        return entry, depleted

    def get_current(self, stock_id: str) -> Optional[Tuple[float, float]]:
        entry = self._data.get(stock_id)
        return (entry["price"], entry["qty"]) if entry else None

    def get_events(self, stock_id: str) -> List[dict]:
        entry = self._data.get(stock_id)
        return entry["events"] if entry else []


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 4:
        print("Usage: py ask_book.py <stock_id> <price> <qty>")
        print("Posts a NEW SELL LIMIT ORDER (Event 4, Step 2.4) for stock_id.")
        sys.exit(1)

    stock_id, price, qty = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    book = AskBook()
    entry = book.post_new_ask(stock_id, price, qty)
    print(f"Posted new sell limit order for {stock_id}: price={price}, qty={qty}")
    print(f"Resulting best ask: price={entry['price']}, total_qty={entry['qty']}")