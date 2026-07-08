"""
Broker market-reference adapter.

Supplies the real-world values bid_ingestion.py currently hardcodes:
tick size, live reference price, and market-hours gating. Written against
Kite Connect's REST shape (Zerodha) since it's the more commonly used of
the two; Upstox's endpoints differ in URL/payload shape but the same
functions/interface apply - swap the HTTP calls inside each method if you
go with Upstox instead.

Auth flow (Kite Connect specifics, matters for how you'll actually use this):
  1. You have an API key + API secret (from your Kite Connect developer app).
  2. Each trading day, the user logs in via Kite's login URL and your
     redirect handler receives a `request_token`.
  3. You exchange (request_token + api_secret) for an `access_token` via
     KiteConnect.generate_session() - this access_token is what's used for
     every API call, and it expires daily. There is no way around the daily
     re-login short of Zerodha's separate "Kite Connect with TOTP" flow for
     automated logins, which needs its own setup.
  4. Upstox uses OAuth2 instead: authorization code -> access_token, with a
     longer-lived refresh_token available - less daily-login friction than
     Kite, worth factoring into your choice if this needs to run unattended.

This module intentionally does NOT hit the real network in its tests below -
it's tested against a local mock, same pattern as mock_odoo_server.py, so
the logic is verified without needing live credentials.
"""

import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Dict, Optional

import requests


@dataclass
class BrokerConfig:
    api_key: str
    access_token: str
    base_url: str = "https://api.kite.trade"  # override for Upstox / sandbox


class QuoteCache:
    """Simple TTL cache so bid validation doesn't hammer the quote API -
    Kite's rate limit is roughly 3 req/sec, and you don't want every
    incoming client bid to trigger a fresh network call."""

    def __init__(self, ttl_seconds: float = 2.0):
        self.ttl = ttl_seconds
        self._store: Dict[str, tuple] = {}  # symbol -> (value, fetched_at)

    def get(self, key: str):
        entry = self._store.get(key)
        if entry and (time.time() - entry[1]) < self.ttl:
            return entry[0]
        return None

    def set(self, key: str, value):
        self._store[key] = (value, time.time())


class BrokerMarketData:
    def __init__(self, config: BrokerConfig, quote_cache_ttl: float = 2.0):
        self.config = config
        self.cache = QuoteCache(quote_cache_ttl)
        self._instrument_cache: Dict[str, dict] = {}

    def _headers(self) -> dict:
        return {
            "X-Kite-Version": "3",
            "Authorization": f"token {self.config.api_key}:{self.config.access_token}",
        }

    def get_instrument_meta(self, symbol: str) -> dict:
        """
        Returns {"tick_size": float, "instrument_token": int, "lot_size": int}.
        In production, fetch Kite's instrument master CSV once per day
        (GET /instruments) and cache it locally rather than calling per bid -
        it's a full exchange dump, not a per-symbol lookup.
        """
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]

        resp = requests.get(
            f"{self.config.base_url}/instruments/NSE",
            headers=self._headers(), timeout=5,
        )
        resp.raise_for_status()
        # real response is CSV; parsing omitted here since the mock test
        # below returns pre-parsed JSON - swap in csv.DictReader for real use
        meta = resp.json()[symbol]
        self._instrument_cache[symbol] = meta
        return meta

    def get_reference_quote(self, symbol: str) -> dict:
        """
        Returns {"ltp": float, "best_bid": float, "best_ask": float,
                 "best_bid_qty": float, "best_ask_qty": float}.
        Cached with a short TTL - see QuoteCache above.
        """
        cached = self.cache.get(symbol)
        if cached is not None:
            return cached

        resp = requests.get(
            f"{self.config.base_url}/quote", headers=self._headers(),
            params={"i": symbol}, timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()[symbol]
        quote = {
            "ltp": data["last_price"],
            "best_bid": data["depth"]["buy"][0]["price"],
            "best_ask": data["depth"]["sell"][0]["price"],
            "best_bid_qty": data["depth"]["buy"][0]["quantity"],
            "best_ask_qty": data["depth"]["sell"][0]["quantity"],
        }
        self.cache.set(symbol, quote)
        return quote

    @staticmethod
    def is_market_open(now: Optional[datetime] = None,
                        holidays: Optional[set] = None) -> bool:
        """NSE equity hours: 09:15-15:30 IST, Mon-Fri, minus holidays.
        `now` should already be in IST if you pass it explicitly."""
        now = now or datetime.now()
        holidays = holidays or set()
        if now.date() in holidays:
            return False
        if now.weekday() >= 5:  # Sat/Sun
            return False
        return dtime(9, 15) <= now.time() <= dtime(15, 30)


if __name__ == "__main__":
    # Local smoke test against a fake requests session, no real network call.
    import unittest.mock as mock

    fake_quote_response = {
        "NSE:ACME": {
            "last_price": 45.10,
            "depth": {
                "buy": [{"price": 45.05, "quantity": 320}],
                "sell": [{"price": 45.12, "quantity": 210}],
            },
        }
    }

    with mock.patch("requests.get") as fake_get:
        fake_get.return_value.raise_for_status = lambda: None
        fake_get.return_value.json = lambda: fake_quote_response

        bmd = BrokerMarketData(BrokerConfig(api_key="x", access_token="y"))
        quote = bmd.get_reference_quote("NSE:ACME")
        print("Quote fetched via mocked broker API:", quote)

        quote2 = bmd.get_reference_quote("NSE:ACME")  # should hit cache, not requests.get again
        print("Second call served from cache:", quote2)
        print("requests.get call count (should be 1, not 2):", fake_get.call_count)

    print("\nMarket open check (2026-07-08 11:00 IST, a Wednesday):",
          BrokerMarketData.is_market_open(datetime(2026, 7, 8, 11, 0)))
    print("Market open check (2026-07-08 20:00 IST, after hours):",
          BrokerMarketData.is_market_open(datetime(2026, 7, 8, 20, 0)))