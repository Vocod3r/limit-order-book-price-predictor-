"""
Test-data generator for exercising bid_ingestion.py without real client
traffic. NOT used by any production path - real bids arrive with real
identity already attached from your platform's own client sessions
(a logged-in user submitting a bid through your app/API), constructed
directly as bid_ingestion.RawBid there. This file exists purely so the
pipeline can be tested end-to-end before real client integration exists.
"""

import random
from datetime import datetime, timedelta
from typing import List, Optional

from bid_ingestion import RawBid


# A small pool of synthetic-but-named "clients", so test output is legible.
# Swap this file out entirely once real client bids are flowing - there is
# no equivalent of this pool in production; real buyer identity comes from
# your own platform's authenticated users, not a generated list.
DEFAULT_BUYER_POOL = [
    "Acme Capital", "Blue Harbor Trading", "Cedar Ridge Partners",
    "Delta Nine Fund", "Everest Asset Mgmt", "Fenwick & Co",
    "Granite Peak Trading", "Halcyon Markets", "Ironwood Capital",
    "Juniper Trading Desk",
]


def generate_random_bids(stock_id: str,
                          n_bids: int,
                          start_time: Optional[datetime] = None,
                          avg_interarrival_seconds: float = 4.0,
                          price_mean: float = 45.00,
                          price_std: float = 0.15,
                          qty_range=(50, 500),
                          buyer_pool: List[str] = None,
                          seed: Optional[int] = None) -> List[RawBid]:
    """Poisson-arrival random bid stream for one stock - test data only."""
    rng = random.Random(seed)
    buyer_pool = buyer_pool or DEFAULT_BUYER_POOL
    t = start_time or datetime.now()

    bids = []
    for _ in range(n_bids):
        t += timedelta(seconds=rng.expovariate(1.0 / avg_interarrival_seconds))
        price = rng.gauss(price_mean, price_std)
        qty = round(rng.uniform(*qty_range))
        buyer = rng.choice(buyer_pool)
        bids.append(RawBid(buyer=buyer, stock_id=stock_id, price=price,
                            quantity=qty, timestamp=t))
    return bids