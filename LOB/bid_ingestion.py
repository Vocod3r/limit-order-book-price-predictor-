"""
Stage 1 (correct scope): validate incoming bids, then register EVERY valid
bid in Odoo CRM - unconditionally, regardless of whether it ever wins an
auction. This is pure ingestion/record-keeping.

The SSM (Stages 2-4, see lob_ssm.py) is a SEPARATE, independent consumer of
the same bid stream. It does its own queue-evolution modeling and price
prediction, but it does not gate what gets written to Odoo here - that
would conflate "is this a valid, recorded bid" (Stage 1's job) with "does
this bid inform a trading decision" (Stage 2-4's job). Two different
questions, two different consumers of the same underlying event stream.
"""

import xmlrpc.client
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass
class RawBid:
    buyer: str
    stock_id: str
    price: float
    quantity: float
    timestamp: datetime
    buyer_email: Optional[str] = None

    @property
    def bid_ref(self) -> str:
        return f"{self.stock_id}:{self.timestamp.isoformat()}:{self.buyer}"


# ---------------------------------------------------------------------------
# Step 2.1: tick-size validation (belongs to Stage 2 in the doc, but runs
# as a gate BEFORE Stage 1 registration - an invalid tick shouldn't be
# recorded as a real bid at all)
# ---------------------------------------------------------------------------

def validate_tick_size(price: float, tick_size: float = 0.01, epsilon: float = 1e-9) -> bool:
    """True if price is a valid multiple of tick_size."""
    remainder = round(price / tick_size) * tick_size
    return abs(price - remainder) < epsilon


def validate_bids(bids: List[RawBid], tick_size: float,
                   round_to_valid: bool = True,
                   market_open: bool = True) -> List[RawBid]:
    """
    Filters out invalid bids. `tick_size` should come from
    BrokerMarketData.get_instrument_meta(symbol)["tick_size"] for real
    instruments (0.05 for most NSE equities) rather than an assumed
    default - a hardcoded 0.01 will reject or mis-round every real bid.

    market_open: pass BrokerMarketData.is_market_open() here. When False,
    all bids are rejected outright - there's no live ask for a real bid
    to clear against outside trading hours, so accepting them into the
    auction would be meaningless (they can still be queued for the next
    session by the caller if that's the desired behavior).

    If round_to_valid=True, instead of rejecting bids with off-tick prices
    (e.g. from float noise), snap them to the nearest valid tick rather
    than discarding real orders - matches how real exchanges normalize
    incoming orders rather than bouncing them for float precision reasons.
    """
    if not market_open:
        return []

    valid = []
    for b in bids:
        if round_to_valid:
            b.price = round(round(b.price / tick_size) * tick_size, 2)
            valid.append(b)
        elif validate_tick_size(b.price, tick_size):
            valid.append(b)
        # else: silently dropped, matching Step 2.1's "rejected before
        # entering the order book"
    return valid


# ---------------------------------------------------------------------------
# Odoo ingestion - unconditional, one record per validated bid
# ---------------------------------------------------------------------------

@dataclass
class OdooConfig:
    url: str
    db: str
    username: str
    api_key: str


class BidIngestionClient:
    """
    Registers every validated bid as a CRM lead in Odoo, per Stage 1:
    "Register buyers and sellers in Odoo CRM. Record: bid price, quantity,
    timestamp, stock identifier." This is unconditional - it happens for
    every bid, win or lose, matched or resting.
    """

    def __init__(self, config: OdooConfig, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run
        self._uid = None
        self._models = None
        self._partner_cache = {}

        if not dry_run:
            self._connect()

    def _connect(self):
        common = xmlrpc.client.ServerProxy(f"{self.config.url}/xmlrpc/2/common")
        self._uid = common.authenticate(
            self.config.db, self.config.username, self.config.api_key, {}
        )
        if not self._uid:
            raise RuntimeError("Odoo authentication failed - check credentials.")
        self._models = xmlrpc.client.ServerProxy(f"{self.config.url}/xmlrpc/2/object")
        print(f"Connected to Odoo as uid={self._uid}")

    def _execute(self, model: str, method: str, *args):
        return self._models.execute_kw(
            self.config.db, self._uid, self.config.api_key, model, method, list(args)
        )

    def ensure_partner(self, name: str, email: Optional[str] = None) -> int:
        if name in self._partner_cache:
            pid = self._partner_cache[name]
            if email:
                # covers the case of a partner already created before an
                # email was ever supplied (e.g. an earlier bid with no
                # email field) - keep it in sync rather than leaving them
                # permanently un-invoiceable.
                self._execute("res.partner", "write", [pid], {"email": email})
            return pid

        existing = self._execute("res.partner", "search", [("name", "=", name)])
        if existing:
            pid = existing[0]
            if email:
                self._execute("res.partner", "write", [pid], {"email": email})
        else:
            create_vals = {"name": name}
            if email:
                create_vals["email"] = email
            pid = self._execute("res.partner", "create", create_vals)

        self._partner_cache[name] = pid
        return pid

    def bid_already_registered(self, bid_ref: str) -> bool:
        existing = self._execute("crm.lead", "search", [("description", "like", bid_ref)])
        return bool(existing)

    def register_bid(self, bid: RawBid) -> dict:
        try:
            if self.dry_run:
                return {
                    "bid_ref": bid.bid_ref, "status": "dry_run",
                    "would_create": {
                        "buyer": bid.buyer, "stock": bid.stock_id,
                        "price": bid.price, "qty": bid.quantity,
                        "timestamp": bid.timestamp.isoformat(),
                    },
                }

            if self.bid_already_registered(bid.bid_ref):
                return {"bid_ref": bid.bid_ref, "status": "skipped_duplicate"}

            partner_id = self.ensure_partner(bid.buyer, bid.buyer_email)
            lead_id = self._execute("crm.lead", "create", {
                "name": f"Bid - {bid.stock_id} @ {bid.price:.2f} x {bid.quantity:.0f}",
                "partner_id": partner_id,
                "type": "lead",  # a bid is a lead, not yet an opportunity -
                                 # it only becomes an opportunity if Stage 5
                                 # actually executes it (separate concern)
                "description": f"bid_ref={bid.bid_ref} price={bid.price} "
                                f"qty={bid.quantity} ts={bid.timestamp.isoformat()}",
            })
            return {"bid_ref": bid.bid_ref, "status": "created", "lead_id": lead_id}

        except Exception as e:
            return {"bid_ref": bid.bid_ref, "status": "error", "error": str(e)}

    def register_batch(self, bids: List[RawBid]) -> dict:
        results = [self.register_bid(b) for b in bids]
        summary = {
            "total": len(results),
            "created": sum(1 for r in results if r["status"] == "created"),
            "dry_run": sum(1 for r in results if r["status"] == "dry_run"),
            "skipped_duplicate": sum(1 for r in results if r["status"] == "skipped_duplicate"),
            "errors": [r for r in results if r["status"] == "error"],
        }
        return {"results": results, "summary": summary}


if __name__ == "__main__":
    from finnhub_market_data import BrokerMarketData, BrokerConfig
    from Test_bid_generation import generate_random_bids  # test data only - see that file's docstring
    from datetime import datetime

    raw_bids = generate_random_bids("ACME_CORP_STOCK", n_bids=50, seed=1)
    print(f"Generated {len(raw_bids)} raw bids (TEST DATA - real bids come from your platform's clients)")

    # Real Finnhub call - no mocking needed anymore, this hits the live API.
    # ACME_CORP_STOCK (the CRM stock_id) and AAPL (the real price-feed
    # symbol) are deliberately different things - the CRM identifier
    # doesn't need to match a real ticker.
    bmd = BrokerMarketData(BrokerConfig(api_key="YOUR_FINNHUB_API_KEY"))
    tick_size = bmd.get_instrument_meta("AAPL")["tick_size"]

    # is_market_open is now an INSTANCE method (needs bmd.client to hit
    # Finnhub's real market-status endpoint) - not a staticmethod anymore.
    market_open = bmd.is_market_open("US")
    print(f"Real tick_size for this instrument: {tick_size}")
    print(f"Market open (real, from Finnhub): {market_open}")

    valid_bids = validate_bids(raw_bids, tick_size=tick_size, market_open=market_open)
    print(f"{len(valid_bids)} bids passed tick-size + market-hours validation")

    client = BidIngestionClient(
        OdooConfig(url="https://example.odoo.com", db="test", username="x", api_key="x"),
        dry_run=True,
    )
    out = client.register_batch(valid_bids)
    print("\nIngestion summary:", out["summary"])
    print("Sample:", out["results"][0])