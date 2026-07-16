import time
import finnhub
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Dict, Optional


@dataclass
class BrokerConfig:
    api_key: str
    access_token: str = ""   # unused, kept for interface parity with Broker_Data.py
    base_url: str = ""       # unused, kept for interface parity


class QuoteCache:
    def __init__(self, ttl_seconds: float = 2.0):
        self.ttl = ttl_seconds
        self._store: Dict[str, tuple] = {}

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
        self.client = finnhub.Client(api_key=config.api_key)
        self.cache = QuoteCache(quote_cache_ttl)
        self._instrument_cache: Dict[str, dict] = {}

    def get_instrument_meta(self, symbol: str) -> dict:
        """
        Returns {"tick_size": float, "instrument_token": str, "lot_size": int}.
        tick_size is hardcoded (see module docstring) - Finnhub's free tier
        has no per-symbol tick-size endpoint.
        """
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]

        meta = {"tick_size": 0.01, "instrument_token": symbol, "lot_size": 1}
        self._instrument_cache[symbol] = meta
        return meta

    def get_reference_quote(self, symbol: str) -> dict:
        """
        Returns {"ltp", "best_bid", "best_ask", "best_bid_qty", "best_ask_qty"} -
        same shape as Broker_Data.py. ltp is real and live; best_bid/best_ask
        are synthesized around it (see module docstring); quantities are
        placeholder constants (real depth isn't available on the free tier).
        """
        cached = self.cache.get(symbol)
        if cached is not None:
            return cached

        quote = self.client.quote(symbol)
        price = quote.get("c")
        if not price:
            raise ValueError(f"No quote data returned for {symbol!r} - check the "
                              f"ticker is valid and covered by Finnhub's free tier.")

        spread = max(price * 0.0005, 0.01)  # ~5bps synthetic spread
        result = {
            "ltp": price,
            "best_bid": round(price - spread, 2),
            "best_ask": round(price + spread, 2),
            "best_bid_qty": 1000,   # placeholder - not real depth
            "best_ask_qty": 1000,   # placeholder - not real depth
        }
        self.cache.set(symbol, result)
        return result

    def is_market_open(self, exchange: str = "US") -> bool:
        """
        REAL market-open status from Finnhub's own market-status endpoint -
        not a guessed clock window like Broker_Data.py's NSE version.
        NOTE: this is an instance method here (needs self.client), unlike
        Broker_Data.py's @staticmethod - if you're calling this as
        BrokerMarketData.is_market_open(...) without an instance, update
        the call site to go through an instance instead.
        """
        status = self.client.market_status(exchange=exchange)
        return bool(status.get("isOpen"))


if __name__ == "__main__":
    import sys
    import unittest.mock as mock

    if len(sys.argv) == 3:
        # Real usage: py finnhub_market_data.py <api_key> <symbol>
        api_key, symbol = sys.argv[1], sys.argv[2]
        bmd = BrokerMarketData(BrokerConfig(api_key=api_key))
        print(f"Live quote for {symbol}: {bmd.get_reference_quote(symbol)}")
        print(f"Market open (US): {bmd.is_market_open('US')}")
    else:
        # Local smoke test, no real network/API key needed
        print("No args given - running local smoke test against a mocked client.\n")
        with mock.patch.object(finnhub.Client, "quote", return_value={"c": 185.40}), \
             mock.patch.object(finnhub.Client, "market_status", return_value={"isOpen": True}):
            bmd = BrokerMarketData(BrokerConfig(api_key="fake"))
            quote = bmd.get_reference_quote("AAPL")
            print("Quote:", quote)
            quote2 = bmd.get_reference_quote("AAPL")
            print("Cache working (should be same object/values):", quote2 == quote)
            print("Market open:", bmd.is_market_open("US"))
            print("Instrument meta:", bmd.get_instrument_meta("AAPL"))