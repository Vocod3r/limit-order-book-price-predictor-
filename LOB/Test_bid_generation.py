"""
Real client bid intake - the actual replacement for Test_bid_generation.py.
A logged-in client's frontend/app calls POST /bids with their real bid;
this constructs a RawBid exactly like the test generator did, but from a
real HTTP request instead of random numbers.

Run:
    pip install flask --break-system-packages
    py bid_api.py <odoo_url> <odoo_db> <odoo_username> <odoo_api_key>

Client-side usage:
    POST /bids
    {
      "buyer": "Acme Capital",
      "stock_id": "AAPL_STOCK",
      "price": 185.40,
      "quantity": 300
    }

buyer identity should really come from your own auth/session layer
(e.g. the logged-in user's name/id), not a free-text field a client can
fake - swap request.json["buyer"] for your session's real user identity
once you have real auth wired up.
"""

import sys
from datetime import datetime

from flask import Flask, request, jsonify

from bid_ingestion import RawBid, validate_bids, BidIngestionClient, OdooConfig
from finnhub_market_data import BrokerMarketData, BrokerConfig

app = Flask(__name__)

FINNHUB_API_KEY = "d9biu5hr01qv2lms14bgd9biu5hr01qv2lms14c0"
STOCK_SYMBOL_MAP = {"AAPL_STOCK": "AAPL", "TSLA_STOCK": "TSLA", "MSFT_STOCK": "MSFT"}
bmd = BrokerMarketData(BrokerConfig(api_key=FINNHUB_API_KEY))


@app.route("/bids", methods=["POST"])
def submit_bid():
    data = request.get_json(force=True)
    try:
        bid = RawBid(
            buyer=str(data["buyer"]),
            stock_id=str(data["stock_id"]),
            price=float(data["price"]),
            quantity=float(data["quantity"]),
            timestamp=datetime.now(),
        )
    except (KeyError, ValueError) as e:
        return jsonify({"status": "error", "error": f"invalid bid payload: {e}"}), 400

    symbol = STOCK_SYMBOL_MAP.get(bid.stock_id, "AAPL")
    tick_size = bmd.get_instrument_meta(symbol)["tick_size"]
    market_open = bmd.is_market_open("US")

    valid_bids = validate_bids([bid], tick_size=tick_size, market_open=True)
    if not valid_bids:
        return jsonify({"status": "rejected", "reason": "failed tick-size/market-hours validation"}), 422

    result = ingestion_client.register_bid(valid_bids[0])
    return jsonify(result), (201 if result["status"] == "created" else 200)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("Usage: py bid_api.py <odoo_url> <odoo_db> <odoo_username> <odoo_api_key>")
        sys.exit(1)

    url, db, username, api_key = sys.argv[1:5]
    ingestion_client = BidIngestionClient(
        OdooConfig(url=url, db=db, username=username, api_key=api_key), dry_run=False
    )
    app.run(host="0.0.0.0", port=5001)